"""Linux: the CPU plan of test runs (resources.py), with their leftovers and the disk checked first, the
extension's Linux Codex, a home reached through a symlink, and the state store's symlink safety on POSIX
file systems."""
from __future__ import annotations
import _isolation  # noqa: F401

import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from _support import StoreCase
from better_codex_context import resources, settings, tokenizer
from better_codex_context.resources import choose
from better_codex_context.store import user_state_home
from better_codex_context.util import ContextError, atomic_write, bundled_binary, read_scoped, token_measure

LINUX = sys.platform.startswith("linux")
SMT = [[0, 4], [1, 5], [2, 6], [3, 7]]  # four cores, two threads each


@unittest.skipUnless(LINUX, "Linux")
class CpuPlanTests(unittest.TestCase):
    def test_all_idle_takes_60_percent_one_thread_per_core_and_core_0_last(self):
        self.assertEqual([1, 2], choose(SMT, {}))  # 60% of 4 cores → 2; never the sibling of an already used thread
        self.assertEqual([1, 2, 3, 4], choose([[c] for c in range(8)], {}))  # no SMT: 60% of 8 → 4, CPU 0 kept free

    def test_busy_cores_leave_one_idle_core_free(self):
        self.assertEqual([2, 3], choose(SMT, {1: 0.9}))  # 3 idle: min(2, 3 - 1)
        self.assertEqual([3], choose(SMT, {1: 0.9, 6: 0.5}))  # 2 idle: min(2, 2 - 1)
        self.assertEqual([0], choose(SMT, {1: 0.9, 6: 0.5, 7: 0.8}))  # 1 idle: still 1 to run on at all
        self.assertEqual([4], choose(SMT, {c: 0.9 for c in range(8)} | {4: 0.4}))  # all busy: the least loaded

    def test_the_quieter_thread_of_a_core_is_used(self):
        self.assertEqual([2, 5], choose(SMT, {1: 0.2}))  # still idle (below 0.3), but 5 is the quieter thread of that core

    def test_the_topology_is_read_from_the_system(self):
        allowed = sorted(os.sched_getaffinity(0))
        cores, source = resources._topology(allowed)
        self.assertIn(source, ("sysfs", "cpuinfo"))
        self.assertEqual(allowed, sorted(cpu for threads in cores for cpu in threads))

    def test_leftover_test_directories_are_removed_and_a_low_disk_reported(self):
        old, fresh, other = (Path(tempfile.mkdtemp(prefix=p)) for p in ("bcc-test-leftover-", "bcc-test-fresh-", "unrelated-"))
        for path in (old, fresh, other):
            self.addCleanup(shutil.rmtree, path, True)
        (old / "output.log").write_bytes(b"x" * 4096)
        hour_ago = time.time() - resources.STALE_SECONDS - 60
        for path in (old, other):
            os.utime(path, (hour_ago, hour_ago))
        removed, freed = resources.clean_stale_directories()
        self.assertEqual((False, True, True), (old.exists(), fresh.exists(), other.exists()))
        self.assertGreaterEqual((removed, freed), (1, 4096))
        plan = resources.plan(interval=0.05) | {"disk_free_share": 0.03}
        self.assertIn("WARNING: only 3.0% of the disk", resources.describe(plan))

    def test_the_extensions_linux_codex(self):
        self.assertRegex(bundled_binary(), r"^bin/linux-(x86_64|aarch64)/codex$")


@unittest.skipUnless(LINUX, "Linux")
class SymlinkedHomeTests(unittest.TestCase):
    """A home reached through a symlink, as on Fedora Silverblue (/home → /var/home) or for root on macOS
    (/var/root, /var → /private/var): the token count, the settings and the state work."""
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="bcc-test-home-")).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        self.real = base / "var" / "home" / "user"
        self.real.mkdir(parents=True)
        (base / "home").symlink_to("var/home")
        environment = patch.dict(os.environ, {"HOME": str(base / "home" / "user")})
        environment.start()
        self.addCleanup(environment.stop)  # also brings back the variables removed here
        for name in ("XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
            os.environ.pop(name, None)
        tokenizer._ranks.cache_clear()  # so the count loads and caches the vocabulary under this home
        self.addCleanup(tokenizer._ranks.cache_clear)

    def test_the_token_count_settings_and_state_work(self):
        self.assertEqual(2, token_measure("hello world")["count"])
        self.assertTrue(any((self.real / ".cache" / "better-codex-context").glob("*.marshal")))
        settings.change({"evidence": "off"})
        self.assertTrue((self.real / ".config" / "better-codex-context" / "config.toml").is_file())
        self.assertEqual(self.real / ".local" / "state" / "better-codex-context", user_state_home())

    def test_a_symlink_inside_the_plugins_own_directory_is_still_refused(self):
        elsewhere = self.real / "elsewhere"
        elsewhere.mkdir()
        (self.real / ".cache").mkdir()
        (self.real / ".cache" / "better-codex-context").symlink_to(elsewhere)
        self.assertEqual(2, token_measure("hello world")["count"])  # counted all the same, just not cached
        self.assertEqual([], list(elsewhere.iterdir()))
        with self.assertRaises(ContextError):
            atomic_write(self.real / ".cache" / "better-codex-context" / "x", b"x")


@unittest.skipUnless(LINUX, "Linux")
class SymlinkSafetyTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_text("outside")

    def test_scoped_reads_reject_parent_paths_symlinks_and_devices(self):
        (self.root / "link").symlink_to(self.outside, target_is_directory=True)
        (self.root / "file-link").symlink_to(self.outside / "secret")
        for path in ("../outside/secret", "link/secret", "file-link", str(self.outside / "secret"), "", "."):
            with self.assertRaises(ContextError, msg=path):
                read_scoped(self.root, path)
        if os.path.exists("/dev/null"):
            with self.assertRaises(ContextError):
                read_scoped(Path("/dev"), "null")
        (self.root / "big").write_bytes(b"x" * 11)
        with self.assertRaises(ContextError):
            read_scoped(self.root, "big", limit=10)

    def test_writes_never_follow_symlinks(self):
        (self.root / "linked").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(ContextError):
            atomic_write(self.root / "linked" / "new.txt", b"data")
        self.assertFalse((self.outside / "new.txt").exists())


if __name__ == "__main__":
    unittest.main()
