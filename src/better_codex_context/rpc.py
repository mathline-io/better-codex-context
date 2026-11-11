"""Local app-server Unix WebSocket transport. No credentials or native patches."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import stat
import struct
import tempfile
import time

from .util import ContextError, canonical
from . import __version__


class UnixRPC:
    def __init__(self, path: str, timeout: float = 10, maximum: int = 1024 ** 3):
        info = Path(path).lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ContextError("native socket must belong to the current user and be a socket, not a symlink")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.maximum, self.timeout, self.sequence = maximum, timeout, 0
        self.buffer = bytearray()
        self.notifications = []
        try:
            self.socket.connect(path)
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buffer:
                data = self.socket.recv(4096)
                if not data:
                    raise EOFError("native handshake closed")
                self.buffer.extend(data)
                if len(self.buffer) > 16384:
                    raise ContextError("native handshake exceeds limit")
            head, remainder = bytes(self.buffer).split(b"\r\n\r\n", 1)
            self.buffer = bytearray(remainder)
            headers = {k.lower(): v.strip() for k, v in (line.split(b":", 1) for line in head.split(b"\r\n")[1:] if b":" in line)}
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            if head.split(b"\r\n", 1)[0].split()[1] != b"101" or headers.get(b"sec-websocket-accept") != expected:
                raise ContextError("native daemon rejected the standard WebSocket handshake")
            initialized = self.call("initialize", {"clientInfo": {"name": "better-codex-context", "version": __version__},
                                                   "capabilities": {"experimentalApi": True}})
            version = re.search(r'\b(0\.\d+\.\d+(?:[-+][\w.-]+)?)', initialized.get('userAgent', ''))
            self.version = version[1] if version else ''
            self.send({"method": "initialized"})
        except BaseException:
            self.socket.close()
            raise

    def exact(self, size):
        while len(self.buffer) < size:
            data = self.socket.recv(min(1024 * 1024, max(4096, size - len(self.buffer))))
            if not data:
                raise EOFError("native daemon disconnected")
            self.buffer.extend(data)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def frame(self, payload: bytes, opcode=1):
        size, mask = len(payload), os.urandom(4)
        head = bytes([0x80 | opcode, 0x80 | size]) if size < 126 else (
            bytes([0x80 | opcode, 0xFE]) + struct.pack('!H', size) if size < 65536 else
            bytes([0x80 | opcode, 0xFF]) + struct.pack('!Q', size))
        self.socket.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def send(self, value):
        self.frame(canonical(value).encode())

    def receive(self):
        with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as message:
            total, started = 0, False
            while True:
                a, b = self.exact(2)
                if a & 0x70 or b & 0x80:
                    raise ContextError("unsupported native WebSocket frame")
                size = b & 127
                if size == 126:
                    size = struct.unpack('!H', self.exact(2))[0]
                elif size == 127:
                    size = struct.unpack('!Q', self.exact(8))[0]
                opcode = a & 15
                if opcode in (8, 9, 10):
                    if size > 125 or not a & 128:
                        raise ContextError("invalid native control frame")
                    body = self.exact(size)
                    if opcode == 8:
                        raise EOFError("native daemon closed the connection")
                    if opcode == 9:
                        self.frame(body, 10)
                    continue
                if opcode not in (0, 1) or (opcode == 0) != started:
                    raise ContextError("invalid native text fragmentation")
                started = True
                total += size
                if total > self.maximum:
                    raise ContextError("native message exceeds explicit size limit; nothing truncated")
                while size:
                    block = self.exact(min(size, 1024 * 1024))
                    message.write(block)
                    size -= len(block)
                if a & 128:
                    message.seek(0)
                    value = json.load(message)
                    if not isinstance(value, dict):
                        raise ContextError("native message must be an object")
                    return value

    def call(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        deadline = time.monotonic() + self.timeout
        self.send({"id": identifier, "method": method, "params": params})
        while time.monotonic() < deadline:
            self.socket.settimeout(max(.01, deadline - time.monotonic()))
            value = self.receive()
            if value.get("id") == identifier and "method" not in value:
                if "error" in value:
                    raise ContextError(f"native {method} rejected (code {value['error'].get('code')}); no automatic retry")
                return value.get("result", {})
            # Bounded metadata only; tool bodies are fetched through explicit pagination.
            if value.get("method") in ("thread/tokenUsage/updated", "thread/compacted"):
                self.notifications.append(value)
                self.notifications = self.notifications[-20:]
        raise TimeoutError("native RPC deadline exceeded; do not replay mutations")

    def close(self):
        self.socket.close()

    def usage(self, thread):
        latest = next((n['params'].get('tokenUsage') for n in reversed(self.notifications)
                       if n.get('method') == 'thread/tokenUsage/updated' and n.get('params', {}).get('threadId') == thread), None)
        self.notifications = [n for n in self.notifications if not (n.get('method') == 'thread/tokenUsage/updated' and n.get('params', {}).get('threadId') == thread)]
        return latest
