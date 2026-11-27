"""Bound invalid evidence navigation without treating rereads as failures."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from nare import Tool, ToolError

from coordinare.services.agent_runtime.errors import refuse_agent
from coordinare.services.environment_attempt import EVIDENCE_PROTOCOL, EnvironmentDiscoveryError

if TYPE_CHECKING:
    from coordinare.services.agent_runtime.accounting import BudgetBridge

__all__ = ['EVIDENCE_PROTOCOL', 'INVALID_EVIDENCE_CODES', 'MAX_INVALID_EVIDENCE_CALLS', 'EvidenceFailureBudget',
           'classified_evidence_tool']

# Per agent session, the model replies that made invalid evidence calls (one strike however many calls a reply made).
MAX_INVALID_EVIDENCE_CALLS = 8
INVALID_EVIDENCE_CODES = frozenset({'agent_evidence_page', 'invalid_tool_arguments'})


def classified_evidence_tool(tool: Tool) -> Tool:
    async def call(arguments: dict[str, Any]) -> Any:
        try:
            return await tool.call(arguments)
        except EnvironmentDiscoveryError as exc:
            if exc.code not in INVALID_EVIDENCE_CODES:
                raise
            raise ToolError(str(exc), code=exc.code) from None

    return Tool(name=tool.name, description=tool.description, input_schema=tool.input_schema, call=call)


class EvidenceFailureBudget:
    def __init__(self, bridge: BudgetBridge, tools: frozenset[str]) -> None:
        self.bridge, self.tools = bridge, tools
        # The session's own key (its identity slot); a bridge without one is charged under its phase.
        self._session_key = bridge.phase_key or bridge.phase

    def _check_accounting(self) -> None:
        if not self.tools:
            return
        budget = self.bridge.budget
        if (budget.agent_evidence_protocol != EVIDENCE_PROTOCOL
                or budget.agent_evidence_errors != len(budget.agent_evidence_error_receipts)):
            refuse_agent('Evidence-call accounting is unavailable or incompatible.', code='agent_evidence_policy')

    def check(self) -> None:
        """Each agent session has its own allowance (MAX_INVALID_EVIDENCE_CALLS); the attempt's call, time and read
        budgets still bound the total."""
        self._check_accounting()
        spent = self.bridge.budget.agent_evidence_phase_errors.get(self._session_key, 0)
        if self.tools and spent >= MAX_INVALID_EVIDENCE_CALLS:
            refuse_agent('Invalid evidence-call allowance exhausted.', code='agent_evidence_call_limit')

    def _error(self, detail: dict[str, Any]) -> dict[str, Any] | None:
        result = detail.get('tool_result')
        if detail.get('tool') not in self.tools or not isinstance(result, dict) or not result.get('is_error'):
            return None
        return result

    @staticmethod
    def _invalid(result: dict[str, Any]) -> bool:
        outcome = result.get('outcome')
        return isinstance(outcome, dict) and (outcome.get('kind') == 'invalid_arguments'
            or (outcome.get('kind') == 'caller_error' and outcome.get('code') in INVALID_EVIDENCE_CODES))

    async def observe(self, detail: dict[str, Any], *, session_id: str, turn: int) -> None:
        if (result := self._error(detail)) is None:
            return
        outcome = result.get('outcome')
        if not isinstance(outcome, dict):
            refuse_agent('Native tool outcome classification is unavailable.', code='agent_tool_outcome_unavailable')
        if not self._invalid(result):
            refuse_agent('Evidence tool failed outside its recoverable protocol.', code='agent_evidence_tool_failed')
        self.check()
        await self._charge(result, session_id=session_id, turn=turn)
        self.check()

    async def reconcile(self, detail: dict[str, Any], *, session_id: str, turn: int) -> None:
        """Account completed refusals queued when dispatch unwinds, without new work.

        Native interruption placeholders are not completed invalid calls. A batch
        may already have completed more refusals than the stopping allowance.
        """
        if (result := self._error(detail)) is not None and self._invalid(result):
            await self._charge(result, session_id=session_id, turn=turn)

    async def _charge(self, result: dict[str, Any], *, session_id: str, turn: int) -> None:
        self._check_accounting()
        call_id = result.get('tool_use_id')
        if not isinstance(call_id, str) or not call_id:
            refuse_agent('Native tool outcome has no identity.', code='agent_tool_outcome_unavailable')
        receipt = hashlib.sha256(json.dumps([session_id, turn, call_id]).encode()).hexdigest()
        budget = self.bridge.budget
        if receipt not in budget.agent_evidence_error_receipts:
            budget.agent_evidence_error_receipts.add(receipt)
            budget.agent_evidence_errors += 1
            # One strike per reply: nare-native-046's shard author sent 20 identical parallel calls in its first reply,
            # was charged 20 against the allowance of 8, and held before it ever saw the refusal.
            turn_receipt = hashlib.sha256(json.dumps([session_id, turn]).encode()).hexdigest()
            if turn_receipt not in budget.agent_evidence_turn_receipts:
                budget.agent_evidence_turn_receipts.add(turn_receipt)
                key = self._session_key
                budget.agent_evidence_phase_errors[key] = budget.agent_evidence_phase_errors.get(key, 0) + 1
            await self.bridge.persist()
