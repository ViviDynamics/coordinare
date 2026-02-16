from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import asyncssh


@dataclass
class AgentSSHService:
    host: str
    user: str
    command: str
    port: int = 22
    key_path: str | None = None
    timeout_seconds: int = 30

    async def _run_remote(self, payload: dict[str, Any]) -> dict[str, Any]:
        cmd = f"{self.command} '{json.dumps(payload)}'"
        conn_args: dict[str, Any] = {"host": self.host, "username": self.user, "port": self.port}
        if self.key_path:
            conn_args["client_keys"] = [self.key_path]
        async with asyncssh.connect(**conn_args) as conn:
            result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=self.timeout_seconds)
        stdout_raw = result.stdout if hasattr(result, "stdout") else ""
        stdout = stdout_raw.strip() if isinstance(stdout_raw, str) else ""
        if not stdout:
            return {"status": "unknown"}
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"status": "unknown", "raw": stdout}

    async def dispatch_card(self, card_context: dict[str, Any]) -> dict[str, Any]:
        payload = {"action": "dispatch", "card_context": card_context}
        return await self._run_remote(payload)

    async def check_health(self) -> dict[str, Any]:
        return await self._run_remote({"action": "health"})

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        payload = {"action": "relay_feedback", "review": review_payload}
        return await self._run_remote(payload)

    async def check_status(self, card_id: str) -> dict[str, Any]:
        payload = {"action": "status", "card_id": card_id}
        return await self._run_remote(payload)
