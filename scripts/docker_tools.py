"""Helpers for querying Docker from CI or management scripts."""
from __future__ import annotations

import subprocess
from typing import List

__all__ = ["list_container_names"]


def list_container_names(include_stopped: bool = False) -> List[str]:
    """
    Return the names of Docker containers visible to the current Docker daemon.

    Works from the host or from inside a container as long as the Docker socket
    is reachable (e.g. via /var/run/docker.sock).
    """
    cmd = ["docker", "ps"]
    if include_stopped:
        cmd.append("-a")
    cmd.extend(["--format", "{{.Names}}"])

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Docker CLI not found in PATH") from exc

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"'docker ps' failed (exit {proc.returncode}): {err}")

    return [name for name in (ln.strip() for ln in proc.stdout.splitlines()) if name]


def main():

    list2 = list_container_names(include_stopped=True)
    print(list2)

if __name__ == "__main__":
    main()
