"""Imported first by every test module: keep tests away from the developer's real configuration.

Codex settings (~/.codex/config.toml), plugin settings (~/.config/better-codex-context), the hook
error log, session-root registry and fallback stores (~/.local/state) all resolve to fresh
temporary directories, and plugin environment variables from the calling shell are dropped.
Activity lines (the hooks' `systemMessage`) are off so other modules can assert exact hook output;
test_activity.py turns them back on.
"""
import os
import tempfile


os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="bcc-test-codex-home-")
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="bcc-test-state-")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="bcc-test-config-")
# Removed when the run ends: every run used to leave these behind (1,165 directories, 183 MB found).
import atexit
import shutil
for _name in ("CODEX_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
    atexit.register(shutil.rmtree, os.environ[_name], True)
for name in [name for name in os.environ if name.startswith("BCC_")]:
    del os.environ[name]
os.environ["BCC_ACTIVITY_LOG"] = "off"
# No sidecar compression runs (it would start `codex exec`) and plain activity text, unless a
# test module turns them on with its own fake codex.
os.environ["BCC_SIDECAR"] = "off"
os.environ["BCC_EVIDENCE"] = "off"  # likewise no evidence summaries (a real codex on PATH would be called)
os.environ["BCC_ACTIVITY_COLOR"] = "off"
# The end-of-turn task-status hold is tested on its own (test_card_status.py); elsewhere a turn's
# hold reason stays exactly what that module asserts.
os.environ["BCC_CARD_UPDATES"] = "off"
# The VS Code extension's variable must not leak in from a developer's VS Code terminal.
os.environ.pop("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", None)

# CPU plan for the run (resources.py): orphans of earlier runs cleaned up, one thread per idle physical core,
# at most 60% of them and always one idle core left to the system. Every process the tests start inherits it.
if not os.environ.get("BCC_TEST_CPUS_BOUND"):
    import sys
    from better_codex_context.resources import bind_for_tests, describe
    _plan = bind_for_tests()
    if _plan:
        os.environ["BCC_TEST_CPUS_BOUND"] = ",".join(map(str, _plan["cpus"]))
        print(describe(_plan), file=sys.stderr)

if os.environ.get("CI"):
    # A hung suite prints every thread's stack and stops, so the job log (and its annotations, see
    # scripts/ci_annotate.py) shows where it hung instead of running until the job timeout.
    import faulthandler
    faulthandler.dump_traceback_later(900, exit=True)
