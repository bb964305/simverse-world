"""Apply per-run Linux limits before replacing this process with Codex."""
from __future__ import annotations

import argparse
import os
import resource
import sys


def _apply_limits(cpu_cores: int, memory_mb: int) -> None:
    if sys.platform != "linux":
        return
    available = sorted(os.sched_getaffinity(0))
    if cpu_cores > len(available):
        raise RuntimeError("requested CPU profile exceeds the available CPU set")
    os.sched_setaffinity(0, set(available[:cpu_cores]))
    memory_bytes = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-cores", type=int, required=True)
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.cpu_cores <= 0 or args.memory_mb <= 0 or not command:
        raise SystemExit("invalid Codex launcher resource profile")
    _apply_limits(args.cpu_cores, args.memory_mb)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
