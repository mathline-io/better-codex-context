#!/usr/bin/env python3
"""Run directly from a plugin checkout/cache without installing a Python package."""
from pathlib import Path
import sys

HOOK = sys.argv[1:2] == ["hook"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from better_codex_context.cli import main
except Exception as exc:  # noqa: BLE001
    if not HOOK:
        raise
    # Codex shows only "hook exited with code 1" for a failing hook, so even a broken install
    # reports its cause as a warning on the hook run instead of exiting non-zero.
    import json
    import traceback
    tail = " | ".join(line.strip() for line in traceback.format_exception(exc)[-3:])
    print(json.dumps({"systemMessage": f"Better Codex Context hook could not start ({Path(__file__).resolve().parents[1]}): "
                                       f"{type(exc).__name__}: {exc}; {tail}"[:2000]}))
    raise SystemExit(0)
raise SystemExit(main())
