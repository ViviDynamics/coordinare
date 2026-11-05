"""A validation carrier must contain current performer and shared code."""
from __future__ import annotations

import json

import pytest
from scripts import environment_validation_image as images


@pytest.mark.parametrize('performer', [None, {'environment_runtime.py': 'stale'}, {'environment_runtime.py': 'current'}])
def test_validation_image_refuses_missing_or_stale_performer_code(monkeypatch, performer):
    monkeypatch.setattr(images, 'package_hashes', lambda: {'runner.py': 'shared-current'})
    monkeypatch.setattr(images, 'performer_hashes', lambda: {'environment_runtime.py': 'current'}, raising=False)
    identity = {'platform': 'linux/aarch64', 'files': {'runner.py': 'shared-current'}}
    if performer is not None:
        identity['performer_files'] = performer
    monkeypatch.setattr(images, '_docker', lambda *args, **kwargs: json.dumps(identity))
    if performer == {'environment_runtime.py': 'current'}:
        result = images.verify_image_code('fixture/image@sha256:'+'a'*64)
        assert result['performer_files'] == performer
    else:
        with pytest.raises(ValueError, match='performer code'):
            images.verify_image_code('fixture/image@sha256:'+'a'*64)
