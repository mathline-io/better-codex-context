"""CPU and memory planning for test runs (tests/_isolation.py, scripts/e2e_smoke.py); not used by the plugin.

Before a run: orphans of earlier test runs are cleaned up (their parent is gone and their command line names
one of the test runs' temporary directories; nothing else is touched), and so are their temporary
directories older than an hour; free disk space below 5% is reported. The CPU load is sampled per logical
CPU, and the run is bound to one hardware thread per physical core (two workers on the sibling threads of
one core run at about half speed). The topology is read from the system, never guessed: sysfs, else
/proc/cpuinfo; when neither tells, two threads per core are assumed and the plan says so. With every core idle it takes at most 60% of the physical cores; with
some of them busy, only idle ones, still at most 60%, and always one idle core fewer, so at least one core
stays free for the system. The core that holds CPU 0 is used last. Linux only: elsewhere the standard
library cannot bind a process, and the test runs are serial anyway.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import tempfile
import time

SHARE = 0.6                # of the physical cores, when every one of them is idle
BUSY = 0.3                 # a logical CPU busier than this over the sample is taken
MARKERS = ("bcc-test-", "bcc-e2e-")   # the temporary directories of this project's test runs
STALE_SECONDS = 3600       # a test run takes a minute or two: older directories are leftovers
LOW_DISK = 0.05            # below this share of free space a run must not just add more


def _cpu_times() -> dict[int, tuple[int, int]]:
    """logical CPU → (busy, total) jiffies since boot."""
    result = {}
    for line in Path("/proc/stat").read_text().splitlines():
        name, *fields = line.split()
        if name.startswith("cpu") and name[3:].isdigit():
            values = [int(v) for v in fields[:8]]
            idle = values[3] + values[4]  # idle + iowait
            result[int(name[3:])] = (sum(values) - idle, sum(values))
    return result


def _process_times() -> dict[int, tuple[int, str]]:
    """pid → (utime + stime jiffies, command name)."""
    result = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        name = stat[stat.find("(") + 1:stat.rfind(")")]
        fields = stat[stat.rfind(")") + 2:].split()
        result[int(entry.name)] = (int(fields[11]) + int(fields[12]), name)
    return result


def _topology(cpus: list[int]) -> tuple[list[list[int]], str]:
    """The physical cores among `cpus`, each as its sibling threads, and where that came from: the kernel's
    sysfs topology, else /proc/cpuinfo (physical id, core id); with neither, 'unknown'."""
    cores = {}
    try:
        for cpu in cpus:
            raw = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list").read_text().strip()
            siblings = set()
            for part in raw.split(","):
                low, _, high = part.partition("-")
                siblings.update(range(int(low), int(high or low) + 1))
            cores.setdefault(tuple(sorted(siblings)), []).append(cpu)
        return [sorted(threads) for _, threads in sorted(cores.items())], "sysfs"
    except (OSError, ValueError):
        cores = {}
    try:
        for block in Path("/proc/cpuinfo").read_text().split("\n\n"):
            fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            fields = {k.strip(): v.strip() for k, v in fields.items()}
            if "processor" in fields and "core id" in fields and int(fields["processor"]) in cpus:
                cores.setdefault((fields.get("physical id", "0"), fields["core id"]), []).append(int(fields["processor"]))
        if cores and sum(map(len, cores.values())) == len(cpus):
            return [sorted(threads) for threads in sorted(cores.values())], "cpuinfo"
    except (OSError, ValueError):
        pass
    return [[cpu] for cpu in cpus], "unknown"


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown CPU"


def clean_stale_directories(now: float | None = None) -> tuple[int, int]:
    """Remove this project's test-run temporary directories older than STALE_SECONDS: (count, bytes freed)."""
    now, removed, freed = now or time.time(), 0, 0
    for entry in Path(tempfile.gettempdir()).iterdir():
        if not entry.name.startswith(MARKERS) or not entry.is_dir() or entry.is_symlink():
            continue
        try:
            if now - entry.stat().st_mtime < STALE_SECONDS:
                continue
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file() and not f.is_symlink())
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed, freed = removed + 1, freed + size
    return removed, freed


def orphans() -> list[tuple[int, str]]:
    """Processes of earlier test runs whose parent is gone: (pid, command line)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            stat = (entry / "stat").read_text()
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        parent = int(stat[stat.rfind(")") + 2:].split()[1])
        if parent == 1 and any(marker in command for marker in MARKERS):
            found.append((int(entry.name), command[:160]))
    return found


def clean_orphans() -> int:
    """Stop the orphans of earlier test runs; returns how many there were."""
    found = orphans()
    for pid, _ in found:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if found:
        time.sleep(1)
        for pid, _ in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return len(found)


def _available_memory_mb() -> int | None:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def choose(cores: list[list[int]], load: dict[int, float]) -> list[int]:
    """One thread per chosen physical core: at most 60% of the cores when all are idle; when some are busy,
    idle ones only, still at most 60% and one fewer than the idle ones; the core of CPU 0 last."""
    idle = [threads for threads in cores if all(load.get(cpu, 0) < BUSY for cpu in threads)]
    budget = max(1, int(len(cores) * SHARE))
    if len(idle) < len(cores):
        budget = max(1, min(budget, len(idle) - 1))  # the machine is busy: keep at least one idle core free
    idle.sort(key=lambda threads: 0 in threads)
    chosen = [min(threads, key=lambda cpu: load.get(cpu, 0)) for threads in idle[:budget]]
    return sorted(chosen or [min((cpu for threads in cores for cpu in threads), key=lambda cpu: load.get(cpu, 0))])


def plan(interval: float = 0.5) -> dict:
    """Which CPUs a test run may use now, and why; with the disk checked and test leftovers removed."""
    cleaned = clean_orphans()
    stale, freed = clean_stale_directories()
    disk = shutil.disk_usage(tempfile.gettempdir())
    allowed = sorted(os.sched_getaffinity(0))
    before, procs_before = _cpu_times(), _process_times()
    time.sleep(interval)
    after, procs_after = _cpu_times(), _process_times()
    load = {cpu: (after[cpu][0] - before[cpu][0]) / max(1, after[cpu][1] - before[cpu][1]) for cpu in allowed if cpu in after}
    cores, topology = _topology(allowed)
    if topology == "unknown":  # siblings cannot be told apart: assume two threads per core, use half as many
        chosen = choose(cores, load)[:max(1, len(choose(cores, load)) // 2)]
    else:
        chosen = choose(cores, load)
    idle = [threads for threads in cores if all(load.get(cpu, 0) < BUSY for cpu in threads)]
    ticks = os.sysconf("SC_CLK_TCK") * interval
    busiest = sorted(((procs_after[pid][0] - procs_before[pid][0]) / ticks, pid, procs_after[pid][1])
                     for pid in procs_after if pid in procs_before)[::-1][:3]
    return {"cpus": chosen, "physical_cores": len(cores), "threads_per_core": max(len(t) for t in cores),
            "busy_cores": len(cores) - len(idle), "busiest": [(name, pid, round(share * 100)) for share, pid, name in busiest
                                                              if share >= BUSY],
            "memory_available_mb": _available_memory_mb(), "orphans_cleaned": cleaned, "topology": topology,
            "cpu_model": cpu_model(), "stale_directories_removed": stale, "freed_mb": freed // (1024 * 1024),
            "disk_free_share": disk.free / disk.total, "disk": tempfile.gettempdir()}


def bind_for_tests() -> dict | None:
    """Plan and bind this process (and everything it starts); None where binding is not available."""
    if not hasattr(os, "sched_setaffinity") or not Path("/proc/stat").exists():
        return None
    result = plan()
    os.sched_setaffinity(0, result["cpus"])
    return result


def describe(result: dict) -> str:
    busy = ", ".join(f"{name} (pid {pid}, {share}%)" for name, pid, share in result["busiest"])
    return (f"tests bound to CPU {','.join(map(str, result['cpus']))}: {len(result['cpus'])} of {result['physical_cores']} "
            f"physical cores ({result['threads_per_core']} threads each, one used per core), {result['busy_cores']} busy"
            + (f" [{busy}]" if busy else "") + f"; {result['memory_available_mb']} MB memory available"
            + (f"; {result['orphans_cleaned']} orphan test processes cleaned up" if result["orphans_cleaned"] else "")
            + (f"; {result['stale_directories_removed']} leftover test directories removed ({result['freed_mb']} MB)"
               if result["stale_directories_removed"] else "")
            + (f"; topology not readable on this system ({result['cpu_model']}): two threads per core assumed"
               if result["topology"] == "unknown" else "")
            + (f"; WARNING: only {result['disk_free_share']:.1%} of the disk of {result['disk']} is free, clean it up"
               if result["disk_free_share"] < LOW_DISK else ""))
