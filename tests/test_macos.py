"""macOS: Codex's daemon is told over its Unix socket to reload the settings (macOS temporary paths are too
long for a socket, so the tests use /tmp), and the VS Code extension's macOS Codex is the one used."""
from __future__ import annotations
import _isolation  # noqa: F401

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from better_codex_context.rpc import UnixRPC
from better_codex_context.upgrade import control_socket, reload_running
from better_codex_context import settings, tokenizer
from better_codex_context.store import user_state_home
from better_codex_context.util import ContextError, bundled_binary, bundled_codexes, token_measure

MACOS = sys.platform == "darwin"


def exact(channel, size):
    data = b''
    while len(data) < size:
        part = channel.recv(size - len(data))
        if not part:
            raise EOFError()
        data += part
    return data


def client_frame(channel):
    a, b = exact(channel, 2)
    size = b & 127
    if size == 126:
        size = struct.unpack('!H', exact(channel, 2))[0]
    if size == 127:
        size = struct.unpack('!Q', exact(channel, 8))[0]
    assert b & 128, 'client frames must be masked'
    mask = exact(channel, 4)
    body = exact(channel, size)
    return a & 15, bytes(c ^ mask[i % 4] for i, c in enumerate(body))


def frame(channel, payload, opcode=1, fin=True):
    size = len(payload)
    header = bytes([(128 if fin else 0) | opcode, size]) if size < 126 else (
        bytes([(128 if fin else 0) | opcode, 126]) + struct.pack('!H', size) if size < 65536 else
        bytes([(128 if fin else 0) | opcode, 127]) + struct.pack('!Q', size))
    channel.sendall(header + payload)


def short_temp():
    """A temporary directory under /tmp: macOS's default one is too long for a Unix socket path
    (104 bytes), the more so once /var resolves to /private/var."""
    return tempfile.TemporaryDirectory(dir="/tmp")


@unittest.skipUnless(MACOS, "macOS")
class RPCTests(unittest.TestCase):
    def server(self, respond):
        temp = short_temp()
        self.addCleanup(temp.cleanup)
        path = str(Path(temp.name) / 'daemon')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen()
        listener.settimeout(10)  # a client that never connects fails the test instead of hanging the suite
        errors = []
        def run():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(4)
                        header = b''
                        while b'\r\n\r\n' not in header:
                            header += connection.recv(1)
                        key = next(line.split(b':', 1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
                        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                        connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                        _, request = client_frame(connection)
                        identifier = json.loads(request)['id']
                        frame(connection, json.dumps({'id': identifier, 'result': {}}).encode())
                        client_frame(connection)  # initialized notification
                        _, request = client_frame(connection)
                        respond(connection, json.loads(request))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        def cleanup():
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
        self.addCleanup(cleanup)
        return path

    def test_socket_symlink_or_regular_file_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'target'
            target.write_text('not a socket')
            link = Path(temp) / 'link'
            link.symlink_to(target)
            for path in (target, link):
                with self.assertRaises(ContextError):
                    UnixRPC(str(path))

    def test_upgrade_asks_the_running_daemon_to_reload_user_config(self):
        seen = []
        def respond(channel, request):
            seen.append(request)
            frame(channel, json.dumps({'id': request['id'], 'result': {}}).encode())
        daemon = Path(self.server(respond))
        home = daemon.parent / 'codex-home'
        link = home / 'app-server-control' / 'app-server-control.sock'
        link.parent.mkdir(parents=True)
        link.symlink_to(daemon)
        self.assertEqual(Path(os.path.realpath(daemon)), control_socket(home))  # macOS: /var is /private/var
        self.assertEqual({'daemon': 'reloaded', 'daemon_version': ''}, reload_running(home))
        self.assertEqual('config/batchWrite', seen[0]['method'])
        self.assertTrue(seen[0]['params']['reloadUserConfig'])

    def test_missing_stale_or_fake_control_socket_reloads_nothing(self):
        with short_temp() as temp:
            home = Path(temp)
            self.assertEqual({'daemon': 'not running'}, reload_running(home))
            path = home / 'app-server-control' / 'app-server-control.sock'
            path.parent.mkdir(parents=True)
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()  # the socket file stays behind with nobody listening
            self.assertIn('unreachable: ConnectionRefusedError', reload_running(home)['daemon'])
            path.unlink()
            path.write_text('not a socket')
            self.assertIsNone(control_socket(home))



@unittest.skipUnless(MACOS, "macOS")
class VarHomeTests(unittest.TestCase):
    def test_a_home_under_var_as_roots_var_root(self):
        """/var is a symlink to /private/var: root's home /var/root and the temporary directories lie under it."""
        home = Path(tempfile.mkdtemp(prefix="bcc-test-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        if home == home.resolve():
            self.skipTest(f"the temporary directory {home} does not go through a symlink here")
        with patch.dict(os.environ, {"HOME": str(home)}):
            for name in ("XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
                os.environ.pop(name, None)
            tokenizer._ranks.cache_clear()
            self.addCleanup(tokenizer._ranks.cache_clear)
            self.assertEqual(2, token_measure("hello world")["count"])
            settings.change({"evidence": "off"})
            self.assertEqual(home.resolve() / ".local" / "state" / "better-codex-context", user_state_home())
        self.assertTrue(any((home / ".cache" / "better-codex-context").glob("*.marshal")))
        self.assertTrue((home / ".config" / "better-codex-context" / "config.toml").is_file())


@unittest.skipUnless(MACOS, "macOS")
class BundledCodexTests(unittest.TestCase):
    def test_the_extensions_macos_codex_is_used_not_its_other_binaries(self):
        home = Path(tempfile.mkdtemp(prefix="bcc-test-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        extension = home / ".vscode" / "extensions" / "openai.chatgpt-26.1021.1-darwin-arm64"
        for binary in (bundled_binary(), "bin/linux-x86_64/codex"):
            (extension / binary).parent.mkdir(parents=True, exist_ok=True)
            (extension / binary).write_text("#!/bin/sh\n")
            (extension / binary).chmod(0o755)
        self.assertRegex(bundled_binary(), r"^bin/macos-(x86_64|aarch64)/codex$")
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual([str(extension / bundled_binary())], bundled_codexes())


if __name__ == '__main__':
    unittest.main()
