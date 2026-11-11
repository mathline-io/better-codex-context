from __future__ import annotations

from datetime import datetime, timezone

import glob
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
import stat
import tempfile
from functools import lru_cache

# Windows has no fork, process groups, signals for timers, fcntl, dir_fd or O_NOFOLLOW; the few places
# that need them branch on this, everything else is the same code on Linux, macOS and Windows.
WINDOWS = os.name == "nt"


def utc_now() -> str:
    """The current UTC time as "2026-11-09 15:20:33": the one form of every BCC timestamp (events, task-card
    statuses and their history, change notes), shown with " UTC" and directly comparable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def python_command(script, *args) -> list[str]:
    """How the plugin starts its own Python entry points. UTF-8 mode on every platform: Windows Python
    otherwise reads and writes text in the locale code page and breaks on Chinese text."""
    return [sys.executable, "-X", "utf8", str(script), *(str(a) for a in args)]


def command_line(argv: list[str]) -> str:
    """argv as one command line for the session's shell, which Codex runs hooks and commands in:
    sh/bash/zsh on Linux and macOS, PowerShell on Windows (cmd /C only for a session without a shell)."""
    if WINDOWS:
        # A quoted executable is a string expression in PowerShell unless preceded by &.
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def detached() -> dict:
    """Popen arguments for a process that outlives the hook that starts it (the sidecar worker)."""
    if WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def own_group() -> dict:
    """Popen arguments for a child whose whole process tree kill_tree can stop (codex exec)."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid: int) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.killpg(pid, signal.SIGKILL)


def pid_alive(pid) -> bool:
    """Whether a process still runs. Never os.kill(pid, 0) on Windows: there it terminates the process."""
    if not pid:
        return False
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, int(pid))  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, owned by someone else
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: not signalled, still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def file_lock(path: Path):
    """An exclusive lock on `path` for the duration of the block (fcntl, or msvcrt on Windows)."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if WINDOWS:
            import msvcrt
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)  # retries for about 10 s, then raises
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
    finally:
        os.close(descriptor)

# Where the VS Code Codex extension (openai.chatgpt) keeps the Codex it bundles:
# <extensions>/openai.chatgpt-<version>-<platform>/bin/<platform>/codex. Remote-SSH and Insiders
# use their own extension folders.
VSCODE_EXTENSIONS = ("~/.vscode/extensions", "~/.vscode-server/extensions", "~/.vscode-insiders/extensions")


def find_codex(name: str = "codex") -> str:
    """The Codex executable to run. `name` on PATH (or as a path) wins. When the default `codex` is not
    on PATH, because Codex is used only through the VS Code extension, the newest Codex bundled with
    the extension is used; it is looked up on every call, so an extension update (a new versioned
    folder) needs no setting change. Returns `name` unchanged when nothing is found, so callers
    report the setting as written."""
    found = shutil.which(name)
    if found or name != "codex":
        return found or name
    bundled = bundled_codexes()
    return bundled[0] if bundled else name


def bundled_binary() -> str:
    """The extension's Codex for this machine, as the extension itself picks it:
    bin/<windows|macos|linux>-<x86_64|aarch64>/codex(.exe). The Windows extension also ships a Linux
    binary (for WSL), which must never be taken for the Windows one."""
    system = "windows" if WINDOWS else "macos" if sys.platform == "darwin" else "linux"
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    return os.path.join("bin", f"{system}-{arch}", "codex.exe" if WINDOWS else "codex")


def bundled_codexes() -> list[str]:
    """The newest Codex bundled with each VS Code install (local, Remote-SSH server, Insiders), newest
    first. Older extension versions stay on disk after an update; only the newest one runs."""
    found = []
    for root in VSCODE_EXTENSIONS:
        # normpath: "~/.vscode/extensions" keeps its "/" on Windows after expanduser
        paths = [path for path in glob.glob(os.path.join(os.path.normpath(os.path.expanduser(root)), "openai.chatgpt-*",
                                                         bundled_binary()))
                 if os.path.isfile(path) and (WINDOWS or os.access(path, os.X_OK))]
        if paths:
            found.append(max(paths, key=os.path.getmtime))
    return sorted(found, key=os.path.getmtime, reverse=True)


class ContextError(ValueError):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def user_directory(variable: str, *default: str) -> Path:
    """A per-user base directory: $variable, else home/default, with the symlinks on the way to it resolved.
    They are the system's or the user's own layout (macOS /var → /private/var, so root's home /var/root;
    Fedora Silverblue /home → /var/home; a ~/.cache moved to another disk), not a redirect planted in the
    plugin's own directories below it, which atomic_write still refuses."""
    return Path(os.environ.get(variable) or Path.home().joinpath(*default)).resolve()


def atomic_write(path: Path, data: bytes) -> None:
    # Reject pre-existing symlinks at every level before creating directories. Callers pass resolved bases
    # (user_directory, Store's resolved root), so a symlink found here is inside the plugin's own directories.
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ContextError("symlink storage is not allowed")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise ContextError("symlink storage is not allowed")
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        if not WINDOWS:  # Windows cannot open a directory to flush it; NTFS journals the rename itself
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_scoped(root: Path, name: str, limit: int = 16 * 1024 * 1024) -> bytes:
    """Walk from an authorized root using directory FDs; reject symlinks and devices."""
    path = Path(name)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise ContextError("file is outside this workspace") from exc
    if not path.parts or any(p in ("..", "") for p in path.parts):
        raise ContextError("invalid file path")
    try:
        fd = _open_scoped(root, path)
    except OSError as exc:
        raise ContextError(f"cannot capture file safely: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ContextError("file is not regular or exceeds the ingest limit")
        with os.fdopen(os.dup(fd), "rb") as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ContextError("file grew beyond the ingest limit")
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ContextError("file changed while being captured")
        return data
    except OSError as exc:
        raise ContextError(f"cannot capture file safely: {exc.strerror}") from exc
    finally:
        os.close(fd)


def _open_scoped(root: Path, path: Path) -> int:
    """A read descriptor for root/path that never follows a symlink. POSIX walks directory descriptors
    (no race between check and open); Windows has no dir_fd, so it checks each step for a link or
    junction and then opens the file."""
    if WINDOWS:
        current = root
        for part in path.parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():  # is_junction: 3.12+
                raise OSError(0, "symbolic link or junction")
        return os.open(current, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    for index, part in enumerate(path.parts):
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if index < len(path.parts) - 1:
            flags |= os.O_DIRECTORY
        try:
            new_fd = os.open(part, flags, dir_fd=fd)
        finally:
            os.close(fd)
        fd = new_fd
    return fd


def token_measure(text: str) -> dict:
    """The one token count of the plugin: o200k_base, exactly as tiktoken counts it (tokenizer.py)."""
    from .tokenizer import count
    return {"count": count(text), "method": "o200k_base", "exact": True}

