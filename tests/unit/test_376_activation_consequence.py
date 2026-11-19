"""nare-native-013 and -018: with the install that creates a virtualenv failed, its missing activation path read as
a fault of its own; each repair author pre-created the directory, and uv then refused the existing directory."""
from __future__ import annotations

import pytest

from tests.unit.test_351_environment_runner import contract


@pytest.mark.asyncio
async def test_an_activation_path_a_failed_install_was_to_create_is_its_consequence(tmp_path) -> None:
    from coordinare_service_inference.environment.runner import run_manifest

    from coordinare.services.environment_repair_triage import triage_failure

    manifest = contract([{'id': 'check', 'argv': ['true']}],
                        install=[{'id': 'install-env', 'argv': ['false']}], activation={'path': ['{cache}/venv/bin']})
    report = await run_manifest(manifest, cache=tmp_path / 'cache', repo=tmp_path, scratch=tmp_path, install=True)
    activation = next(result for result in report.results if result.id == 'activation')
    assert activation.status == 'not_run' and 'install-env' in activation.output
    triage = triage_failure(manifest, report)
    assert set(triage.roots) == {'install-env'} and 'activation' in triage.consequences


@pytest.mark.asyncio
async def test_a_missing_activation_path_with_every_install_passed_is_still_a_failure(tmp_path) -> None:
    from coordinare_service_inference.environment.runner import run_manifest

    manifest = contract([{'id': 'check', 'argv': ['true']}],
                        install=[{'id': 'install-env', 'argv': ['true']}], activation={'path': ['{cache}/venv/bin']})
    report = await run_manifest(manifest, cache=tmp_path / 'cache', repo=tmp_path, scratch=tmp_path, install=True)
    activation = next(result for result in report.results if result.id == 'activation')
    assert activation.status == 'fail' and activation.output == 'declared activation path missing'
