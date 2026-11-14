"""Durable reservations and exact result receipts for explicitly owned actions."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from coordinare.services.agent_runtime.checkpoint import EffectReceipt
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from coordinare.services.agent_runtime.models import JsonObject


class EffectJournal:
    def __init__(self, receipts: Sequence[EffectReceipt] = ()) -> None:
        self._receipts = {item.operation_id: item for item in receipts}
        self.persist: Callable[[], Awaitable[None]] | None = None
        self.failure: str | None = None

    def receipts(self) -> tuple[EffectReceipt, ...]:
        return tuple(self._receipts[key] for key in sorted(self._receipts))

    def restore(self, receipts: Sequence[EffectReceipt]) -> None:
        self._receipts = {item.operation_id: item for item in receipts}

    async def ensure_settled(self) -> None:
        code = self.failure or ('agent_effect_pending' if any(
            item.status == 'pending' for item in self._receipts.values()) else None)
        if code:
            raise EnvironmentDiscoveryError('External action requires reconciliation.', code=code)

    async def _flush(self) -> None:
        try:
            if self.persist is None:
                raise RuntimeError('No durable action checkpoint')
            await self.persist()
        except Exception:
            self.failure = 'agent_effect_persistence'
            raise EnvironmentDiscoveryError('External action receipt could not be persisted.',
                                            code=self.failure) from None

    async def run(self, tool: str, binding_digest: str, execute: Callable[[], Awaitable[JsonObject]]) -> JsonObject:
        await self.ensure_settled()
        key = hashlib.sha256(f'{tool}:{binding_digest}'.encode()).hexdigest()
        if (saved := self._receipts.get(key)) is not None:
            if saved.result_json is None:
                raise EnvironmentDiscoveryError('Completed action has no reusable result.', code='agent_effect_result_missing')
            result: JsonObject = json.loads(saved.result_json)
            return result
        self._receipts[key] = EffectReceipt(operation_id=key, tool=tool, binding_digest=binding_digest, status='pending')
        await self._flush()
        result = await execute()
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self._receipts[key] = EffectReceipt(operation_id=key, tool=tool, binding_digest=binding_digest,
            status='completed', result_digest=hashlib.sha256(serialized.encode()).hexdigest(), result_json=serialized)
        await self._flush()
        return result
