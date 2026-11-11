"""Windows: hooks run in PowerShell, the VS Code extension's codex.exe is used (never the Linux binary it
ships for WSL), checking a process never stops it, and an AGENTS.md checked out with CRLF line ends."""
from __future__ import annotations
import _isolation  # noqa: F401

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from _support import StoreCase
from better_codex_context import instructions, util
from better_codex_context.materials import recall

WINDOWS = os.name == "nt"


@unittest.skipUnless(WINDOWS, "Windows")
class WindowsTests(unittest.TestCase):
    def test_powershell_executes_quoted_executable_and_literal_arguments(self):
        # Apostrophes, spaces and dollar signs must survive PowerShell parsing unchanged.
        command = util.command_line([sys.executable, "-c", "import sys;print(sys.argv[1])", "a b'$value"])
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual("a b'$value", result.stdout.strip())

    def test_the_extensions_codex_exe_is_used_never_its_linux_binary(self):
        home = Path(tempfile.mkdtemp(prefix="bcc-test-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        extension = home / ".vscode" / "extensions" / "openai.chatgpt-26.1021.1-win32-x64"
        for binary in (util.bundled_binary(), os.path.join("bin", "linux-x86_64", "codex")):
            (extension / binary).parent.mkdir(parents=True, exist_ok=True)
            (extension / binary).write_text("")
        self.assertTrue(util.bundled_binary().endswith("codex.exe"))
        with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):  # ~ is USERPROFILE on Windows
            found = util.bundled_codexes()
        self.assertEqual([os.path.normcase(extension / util.bundled_binary())], [os.path.normcase(p) for p in found])

    def test_checking_a_process_never_stops_it(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        self.assertTrue(util.pid_alive(child.pid))
        self.assertIsNone(child.poll())  # still running: os.kill(pid, 0) would have terminated it
        child.kill()
        child.wait(10)
        self.assertFalse(util.pid_alive(child.pid))


@unittest.skipUnless(WINDOWS, "Windows")
class CrlfInstructionsTests(StoreCase):
    def test_an_agents_md_with_crlf_line_ends_is_cut_and_restored(self):
        self.codex_config("project_doc_max_bytes = 1024\n")
        filler = "".join(f"## Background {n}\n\n" + "Plain description of the module layout.\n" * 12 + "\n"
                         for n in range(6))
        text = "# Project rules\n\n" + filler + "## Deployment\n\n- Never deploy on Fridays.\n"
        (self.root / "AGENTS.md").write_bytes(text.replace("\n", "\r\n").encode())
        block = instructions.overflow(self.store, budget=2000)
        self.assertIn("Never deploy on Fridays.", block["text"])
        self.assertNotIn("\r", block["text"])
        ref = block["text"].split("§ Deployment", 1)[1].split('ref="', 1)[1].split('"', 1)[0]
        self.assertTrue(recall(self.store, ref)["text"].startswith("## Deployment"))


if __name__ == "__main__":
    unittest.main()
