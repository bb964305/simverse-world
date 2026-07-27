"""Join a controller-owned cgroup and drop to the run's dedicated UID."""
from __future__ import annotations

import argparse
import os
import resource


def _enter_isolation(*, uid: int, gid: int, cgroup_path: str, cwd: str) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Codex launcher must start as root")
    with open(os.path.join(cgroup_path, "cgroup.procs"), "w", encoding="ascii") as fd:
        fd.write(str(os.getpid()))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.chdir(cwd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--cgroup-path", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.uid < 10_000 or args.gid < 10_000 or not command:
        raise SystemExit("invalid Codex launcher isolation profile")
    _enter_isolation(
        uid=args.uid,
        gid=args.gid,
        cgroup_path=args.cgroup_path,
        cwd=args.cwd,
    )
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
