"""Routing-table models for the self-hosted backend robustness layer (spec 078).

The models live in the shared ``coordinare-routing-models`` package (issue #228) so the
coordinare daemon can validate dashboard routing writes without installing the performer.
This module re-exports them under the historical ``performer.proxy.routing`` path. The
objects are the same ones, not copies.

It also points the shared validator at this package's live ``NORMALIZER_REGISTRY``, so a
``normalize`` or ``translate`` target is checked against the normalizers the performer
actually has. See ``coordinare_routing_models.routing`` for the rules.
"""

from __future__ import annotations

from collections.abc import Collection

from coordinare_routing_models import (
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
from coordinare_routing_models.routing import _normalize_backend

from .normalizers import NORMALIZER_REGISTRY


def _performer_normalizer_keys() -> Collection[str]:
    # Looked up on each call, so a monkeypatched NORMALIZER_REGISTRY is the one validated.
    return NORMALIZER_REGISTRY


set_normalizer_key_source(_performer_normalizer_keys)

__all__ = [
    "BUILTIN_NORMALIZER_KEYS",
    "HealthProbe",
    "NORMALIZER_REGISTRY",
    "RoutingEntry",
    "RoutingTable",
    "Strategy",
    "TargetDescriptor",
    "WireFormat",
    "_normalize_backend",
    "registered_normalizer_keys",
]
