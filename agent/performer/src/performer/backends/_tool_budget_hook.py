"""Claude PreToolUse hook: count admitted calls atomically, stop at the cap.

Invoked as a standalone Python script so no performer logging reaches stdout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    directory = Path(sys.argv[1])
    reason = "Tool call budget could not be enforced"
    try:
        import fcntl

        sys.stdin.buffer.read()  # drain the CLI payload before exiting
        limit = int(sys.argv[2])
        if limit < 1:
            raise ValueError("invalid tool budget")
        with (directory / "count").open("a+") as counter:
            fcntl.flock(counter, fcntl.LOCK_EX)
            counter.seek(0)
            used = int(counter.read() or "0")
            if used < 0:
                raise ValueError("invalid counter")
            if used < limit:
                counter.seek(0)
                counter.truncate()
                counter.write(str(used + 1))
                counter.flush()
                return
        reason = f"Tool call budget exhausted ({limit})"
    except Exception:
        pass
    try:
        (directory / "exhausted").touch()
    except OSError:
        pass
    print(json.dumps({
        "continue": False,
        "stopReason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))


if __name__ == "__main__":
    main()
