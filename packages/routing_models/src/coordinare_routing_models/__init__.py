"""Spec-078 self-hosted routing-table models, shared by the daemon and the performer (issue #228).

The daemon validates dashboard routing writes against these models, and the production
daemon image does not install the performer package. The performer re-exports the same
objects from ``performer.proxy.routing``, so both sides enforce one rule set.
"""

from __future__ import annotations

from coordinare_routing_models.routing import (
    BUILTIN_NORMALIZER_KEYS,
    HealthProbe,
    RoutingEntry,
    RoutingTable,
    Strategy,
    TargetDescriptor,
    WireFormat,
    registered_normalizer_keys,
    set_normalizer_key_source,
)

__all__ = [
    "BUILTIN_NORMALIZER_KEYS",
    "HealthProbe",
    "RoutingEntry",
    "RoutingTable",
    "Strategy",
    "TargetDescriptor",
    "WireFormat",
    "registered_normalizer_keys",
    "set_normalizer_key_source",
]
