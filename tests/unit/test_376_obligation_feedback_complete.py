"""nare-native-015: a repair author told of one removed install at a time spent all eight schema corrections."""
from __future__ import annotations

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError


def install(command_id: str) -> dict[str, object]:
    return {'id': command_id, 'argv': ['true'], 'required': True}


def test_every_removed_install_is_named_in_one_refusal() -> None:
    from coordinare.services import environment_discovery as discovery

    ledger = discovery._ObligationLedger()
    ledger.record({'install': [install(name) for name in ('uv', 'system', 'image', 'tmpdir')], 'checks': []})
    with pytest.raises(EnvironmentDiscoveryError) as refused:
        ledger.record({'install': [install('uv')], 'checks': []})
    assert refused.value.code == 'obligation_removed'
    assert str(refused.value) == 'required installation obligation removed: system, image, tmpdir'
    ledger.record({'install': [install(name) for name in ('uv', 'system', 'image', 'tmpdir')], 'checks': []})
