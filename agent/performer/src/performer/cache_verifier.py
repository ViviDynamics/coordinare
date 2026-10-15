"""Trusted verifier entry point. Only a bounded summary is written to pod logs."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from coordinare_service_inference.cache_provenance import RESULT_SENTINEL, WalkRefusedError, walk_summary

ERROR_MAX_CHARS = 1000
CLAIM_PERMISSION_REASON = (
    "claim PermissionError: make files world-readable and directories world-traversable "
    "(mode o+rX); verifier runs as uid 10001 with no fsGroup"
)


def main() -> None:
    try:
        config = json.loads(os.environ["PROVENANCE_CONFIG"])
        activate = (base64.b64decode(config["activate_sh_b64"]).decode("utf-8")
                    if "activate_sh_b64" in config else config["activate_sh"])
        result = walk_summary(Path(os.environ["DEVENV"]), activate,
                              max_entries=config["max_entries"], max_bytes=config["max_bytes"])
    except PermissionError:
        result = {"error": CLAIM_PERMISSION_REASON, "transient": False}
    except WalkRefusedError as exc:
        result = {"error": str(exc)[:ERROR_MAX_CHARS], "transient": False}
    except Exception as exc:
        result = {"error": type(exc).__name__, "transient": True}
    sys.stdout.write(RESULT_SENTINEL + json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
