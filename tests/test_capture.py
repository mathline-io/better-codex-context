"""`bcc capture -- CMD`: exact archived output, a short preview, and honest exit codes."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import sys
import unittest

from _support import StoreCase
from better_codex_context.capture import capture_command


class CaptureCommandTests(StoreCase):
    def parts(self, ref, stream):
        manifest = json.loads(self.store.artifact(ref)[1])
        return b"".join(self.store.artifact(part)[1] for part in manifest[stream + "_parts"])

    def test_wrapper_keeps_exact_streams_and_returns_a_small_preview(self):
        # Raw bytes, so the child's own text layer (CRLF, a Windows code page) cannot alter them.
        script = ("import sys; sys.stdout.buffer.write(b'progress\\n'*20000 + b'\\nERROR exact failure\\n'); "
                  "sys.stderr.buffer.write('原始错误🙂'.encode()); sys.exit(7)")
        result = capture_command(self.store, [sys.executable, "-c", script])
        self.assertEqual((7, False), (result["exit_code"], result["uncertain"]))
        self.assertLess(len(result["preview"]), 6000)
        self.assertIn("ERROR exact failure", result["preview"])
        self.assertIn("Receipt/original stream index: " + result["ref"], result["preview"])
        self.assertEqual("原始错误🙂".encode(), self.parts(result["ref"], "stderr"))
        self.assertEqual(("progress\n" * 20000 + "\nERROR exact failure\n").encode(), self.parts(result["ref"], "stdout"))
        meta = self.store.artifact_metadata(result["ref"])["metadata"]
        self.assertEqual((7, "capture-wrapper", False), (meta["exit_code"], meta["tool_name"], meta["execution_uncertain"]))


if __name__ == "__main__":
    unittest.main()
