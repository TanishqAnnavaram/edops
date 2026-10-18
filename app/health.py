"""Liveness for processes without HTTP: the main loop touches a file, the healthcheck reads its age.

Run as `python -m app.health` from a container healthcheck.
"""

import os
import sys
import time
from pathlib import Path

MAX_AGE_SECONDS = 15


def beat() -> None:
    path = os.environ.get("HEALTH_FILE")
    if path:
        Path(path).touch()


def main() -> int:
    path = Path(os.environ.get("HEALTH_FILE", "/tmp/health"))
    if not path.exists():
        print(f"{path} missing: main loop has not started", file=sys.stderr)
        return 1
    age = time.time() - path.stat().st_mtime
    if age > MAX_AGE_SECONDS:
        print(f"{path} is {age:.0f}s old: main loop is stuck", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
