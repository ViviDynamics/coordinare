"""nare-native-022: every cp -a of the project into {scratch} failed in the installer on Docker Desktop.

The private runtime carrier's /tmp (which holds {scratch}) was a host bind. Docker Desktop file sharing refuses an
owner's chmod of a directory that is not owner-writable, which cp -a needs on the copy it creates. The carrier's /tmp
is now an anonymous volume: on Docker's own storage, private to the carrier and removed with it.
"""
from __future__ import annotations

from typing import Any


def test_the_carrier_tmp_is_an_anonymous_volume_never_a_host_path(tmp_path: Any) -> None:
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from coordinare.services.environment_runtime import CARRIER_TMP_MOUNT, runtime_verifier_options
    from tests.unit.runtime_manifest_fixture import runtime_cache

    manifest = EnvironmentManifest.model_validate(runtime_cache(tmp_path).environment_manifest)
    options = runtime_verifier_options(manifest)
    assert options[options.index('--mount') + 1] == CARRIER_TMP_MOUNT == 'type=volume,target=/tmp'
    # Anonymous: no source, so no host directory and no named volume left behind by the --rm carrier.
    assert 'source=' not in CARRIER_TMP_MOUNT
    assert not any(item.endswith(':/tmp') for item in options)
    assert not (tmp_path / 'runtime-scratch').exists()
