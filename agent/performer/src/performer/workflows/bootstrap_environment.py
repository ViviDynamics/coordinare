"""Execution primitive for bootstrap gates and coordinare-owned cache artifacts."""
from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from performer.models import Score, Stand


class BootstrapEnvironment:
    """Keep trusted artifact fingerprints outside the installer-writable cache."""

    def __init__(self, stand: Stand, score: Score) -> None:
        self.stand = stand
        self.score = score
        self.artifacts: dict[str, tuple[int, int, bytes]] = {}
        self.inference: dict[str, Any] = {}

    def _fingerprint(self, name: str) -> tuple[int, int, bytes]:
        path = Path(self.score.env_cache_path) / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            raise ValueError("artifact must be a regular file under 2 MB")
        return info.st_dev, info.st_ino, hashlib.sha256(path.read_bytes()).digest()

    async def run(self, step: str) -> dict[str, Any]:
        """Execute one gate. Only verification/readiness failures are repairable."""
        from performer import workspace
        from performer.config import get_settings
        from performer.main import _read_declared_services, _run_service_inference

        if step == "snapshot":
            if not self.score.env_cache_path or not Path(self.score.env_cache_path).is_absolute():
                return {"passed": False, "reason": "bootstrap requires an absolute env_cache_path"}
            for name, promised in (("verify.sh", self.score.verify_provided),
                                   ("activate.sh", self.score.activate_provided)):
                if promised:
                    try:
                        self.artifacts[name] = self._fingerprint(name)
                    except (OSError, ValueError):
                        return {"passed": False, "reason": f"missing or invalid promised artifact: {name}"}
            return {"passed": True}
        if step == "integrity":
            for name, expected in self.artifacts.items():
                try:
                    unchanged = self._fingerprint(name) == expected
                except (OSError, ValueError):
                    unchanged = False
                if not unchanged:
                    return {"passed": False, "reason": f"coordinare-owned artifact changed: {name}"}
            return {"passed": True}
        if step == "inference":
            try:
                self.inference = await asyncio.wait_for(
                    _run_service_inference(self.stand.path, self.score.env_cache_path),
                    timeout=get_settings().SERVICE_INFERENCE_TIMEOUT,
                )
            except TimeoutError:
                self.inference = {"inference_succeeded": False, "inference_skipped_reason": "timeout"}
            return {"passed": True, "inference": self.inference}
        if step == "readiness":
            if not self.score.coordinare_manages_services:
                return {"passed": True}
            passed, failures = await workspace.run_service_readiness(
                self.score.env_cache_path, self.stand.cache_env,
                _read_declared_services(self.stand.path), self.inference,
            )
            return {"passed": passed, "reason": "; ".join(f["reason"] for f in failures)[-1000:]}
        if step == "verify":
            integrity = await self.run("integrity")
            if not integrity["passed"]:
                return {**integrity, "integrity_failure": True}
            passed, detail = await workspace.run_env_cache_verify(
                self.score.env_cache_path, self.stand.cache_env,
            )
            integrity = await self.run("integrity")
            if not integrity["passed"]:
                return {**integrity, "integrity_failure": True}
            return {"passed": passed is True, "reason": detail[-1000:]}
        raise ValueError(f"unknown bootstrap step: {step}")
