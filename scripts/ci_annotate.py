"""Turn unittest failures, and a faulthandler dump of a hung suite, into GitHub annotations.

Annotations are public (job logs need repository admin rights to download); GitHub shows at most
10 per step, so each failure becomes one annotation with its exception line and the innermost
non-test frame.
"""
import re
import sys

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
notes = []
for block in re.split(r"\n={20,}\n", log.split("\nTimeout (")[0]):
    head = re.match(r"(FAIL|ERROR): (\w+) \(([\w.]+)\)", block.strip())
    if not head:
        continue
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    at = next((i for i in range(len(lines) - 1, -1, -1)
               if re.match(r"[\w.]*(Error|Exception|Exit|Interrupt|Deadline)\b", lines[i])), len(lines) - 1)
    error = " / ".join(lines[at:at + 4])  # a multi-line assertion (set or list differences) continues below
    frames = [l for l in lines if l.startswith('File "') and "/tests/" not in l.replace("\\", "/")]
    notes.append((head.group(3).rsplit(".", 1)[0], f"{head.group(1)} {head.group(2)}: {error}" + (f" | {frames[-1]}" if frames else "")))
if "Timeout (" in log and "Thread 0x" in log:  # faulthandler.dump_traceback_later: the suite hung
    dump = log[log.index("Timeout ("):]
    frames = re.findall(r'File "([^"]+)", line (\d+) in (\w+)', dump)
    notes.insert(0, ("hang", "suite hung; innermost frames first: "
                     + "; ".join(f"{path.replace(chr(92), '/').rsplit('/', 1)[-1]}:{line} {name}" for path, line, name in frames[:14])))
if len(notes) > 10:  # GitHub shows 10 per step: the last one names every failure that has no annotation of its own
    notes[9:] = [("more", f"{len(notes) - 9} more: " + "; ".join(f"{t} {x.split(':', 1)[0]}" for t, x in notes[9:]))]
for title, text in notes:
    print(f"::error title={title}::{text[:900]}")
