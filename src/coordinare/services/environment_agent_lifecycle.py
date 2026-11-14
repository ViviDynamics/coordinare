"""Daemon-owned native sessions outside all performer-visible mounts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.errors import refuse_agent
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.bootstrap_history import record_activity
from coordinare.services.environment_agent import EnvironmentAgentContext
from coordinare.services.environment_repair import journal_for, mark_native_attempt

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.agent_runtime.configuration import AgentModelConfig


def bootstrap_agent_context(*, model: AgentModelConfig, cache: EnvCacheState, state: Mapping[str, Any],
                            config: Any, performer_mounts: Sequence[Path]) -> EnvironmentAgentContext:
    journal = journal_for(cache)
    identity = json.dumps([cache.symphony_name, journal.source_commit, journal.started_at, journal.generation], separators=(',', ':'))
    attempt_id = hashlib.sha256(identity.encode()).hexdigest()
    state_file = getattr(config, 'state_file_path', None)
    if state_file is None:
        refuse_agent('Private agent checkpoint directory is unavailable.', code='agent_checkpoint_unavailable')
    root = Path(state_file).expanduser().parent / 'bootstrap-agents' / attempt_id
    private_root = root.resolve()
    if any(private_root.is_relative_to(mount.expanduser().resolve())
           or mount.expanduser().resolve().is_relative_to(private_root) for mount in performer_mounts):
        refuse_agent('Agent checkpoint directory overlaps a performer mount.', code='agent_checkpoint_exposed')
    model_digest = hashlib.sha256(json.dumps(asdict(model), sort_keys=True).encode()).hexdigest()
    if mark_native_attempt(cache):
        record_activity(state, cache.symphony_name, 'bootstrap_agent',
            'Starting a fresh native phase from validated domain state; prior conversation is diagnostic only.')
    if journal.trial_usage_unknown:
        record_activity(state, cache.symphony_name, 'bootstrap_agent',
            'Prior trial usage was not recorded; its unprovable remaining allowance is held.')

    async def progress(event: dict[str, Any]) -> None:
        text = (f"{event['phase']} {event['role']}: {event['kind']}, turn {event['turn']}, "
                f"{event['calls']} calls, {event.get('evidence_remaining', 0)} evidence pages remaining")
        if event.get('hold_code'):
            text += f"; {event['hold_code']}"
        record_activity(state, cache.symphony_name, 'bootstrap_agent', text)

    return EnvironmentAgentContext(runtime=NareRuntime(config=model, calls=state.get('conductor_calls')),
        attempt_id=attempt_id, model_config_digest=model_digest, checkpoint_store=CheckpointStore(root), event_sink=progress)
