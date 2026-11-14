"""Bind the existing conductor selection to a supported native transport."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nare.transport import Transport

    from coordinare.config import ConductingConfig


@dataclass(frozen=True)
class AgentModelConfig:
    provider: Literal['openai', 'anthropic']
    model: str
    max_tokens: int
    temperature: float | None
    effort: Literal['low', 'medium', 'high'] | None
    base_url: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)

    def transport(self, instructions: str) -> Transport:
        from nare import make_transport

        return make_transport(self.provider, model=self.model, base_url=self.base_url,
                              api_key=self.api_key, max_tokens=self.max_tokens,
                              temperature=self.temperature, effort=self.effort, system=instructions)


def resolve_conducting(config: ConductingConfig, *, environ: Mapping[str, str]) -> AgentModelConfig:
    """Preserve bootstrap's conductor configuration; refuse CLI-only backends."""
    if config.backend not in {'openai_api', 'anthropic_api'}:
        message = 'Bootstrap requires a supported API transport in conducting configuration.'
        raise EnvironmentDiscoveryError(message, code='agent_transport_unsupported')
    anthropic = config.backend == 'anthropic_api'
    if anthropic and config.temperature is not None:
        message = 'The native Anthropic transport does not support temperature.'
        raise EnvironmentDiscoveryError(message, code='agent_parameters_unsupported')
    key = environ.get(config.api_key_env or ('ANTHROPIC_API_KEY' if anthropic else 'OPENAI_API_KEY'))
    if not key:
        message = 'The configured bootstrap API credential is missing.'
        raise EnvironmentDiscoveryError(message, code='agent_credentials_missing')
    return AgentModelConfig(
        provider='anthropic' if anthropic else 'openai',
        model=config.model or ('claude-sonnet-4-20250514' if anthropic else 'gpt-4o-mini'),
        max_tokens=config.max_tokens, temperature=config.temperature,
        # ConductingConfig explicitly documents effort as a no-op on anthropic_api.
        effort=None if anthropic else config.effort,
        base_url=config.base_url,
        api_key=key,
    )
