"""Build argv for the process-tree supervisor in source and frozen modes."""
from __future__ import annotations

import sys
from pathlib import Path


DISPATCH_FLAG = "--imece-process-supervisor"


def supervisor_argv(*args: str) -> tuple[str, ...]:
    """Return an argv invoking the supervisor without script paths in frozen apps."""
    if getattr(sys, "frozen", False):
        return (sys.executable, DISPATCH_FLAG, *args)
    return (sys.executable, str(Path(__file__).with_name("supervisor.py")), *args)
