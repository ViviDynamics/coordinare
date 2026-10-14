"""Issue #228: ``performer.proxy.routing`` re-exports the shared routing models, not copies.

The daemon and the performer must enforce one rule set, so every name the performer imports from
``performer.proxy.routing`` has to be the object ``coordinare_routing_models`` defines. The
normalizer key set also has to match the live registry, because the shared validator uses
``BUILTIN_NORMALIZER_KEYS`` wherever the performer is not installed.
"""

from __future__ import annotations

import coordinare_routing_models as shared
import pytest
from coordinare_routing_models import routing as shared_routing
from performer.proxy import routing as performer_routing
from performer.proxy.normalizers import NORMALIZER_REGISTRY

_RE_EXPORTED = [
    "RoutingEntry",
    "RoutingTable",
    "TargetDescriptor",
    "WireFormat",
    "Strategy",
    "HealthProbe",
    "registered_normalizer_keys",
]


@pytest.mark.parametrize("name", _RE_EXPORTED)
def test_performer_name_is_the_shared_object(name: str) -> None:
    assert getattr(performer_routing, name) is getattr(shared, name)


def test_normalize_backend_is_the_shared_function() -> None:
    assert performer_routing._normalize_backend is shared_routing._normalize_backend


def test_builtin_normalizer_keys_match_the_performer_registry() -> None:
    # A new normalizer must be added to BUILTIN_NORMALIZER_KEYS, or a daemon without the
    # performer rejects a routing entry that the performer would accept.
    assert frozenset(NORMALIZER_REGISTRY) == shared.BUILTIN_NORMALIZER_KEYS


def test_performer_validator_reads_the_live_registry() -> None:
    assert performer_routing.registered_normalizer_keys() == frozenset(NORMALIZER_REGISTRY)
