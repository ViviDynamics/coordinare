"""A local platform build still needs a verified immutable image identity."""
from __future__ import annotations

import pytest
from scripts import environment_validation_image as images


def test_explicit_local_base_rejects_identity_mismatch_before_creating_resources(tmp_path, monkeypatch):
    def inspect(*args, **kwargs):
        assert args[:2] == ('image', 'inspect')
        return 'sha256:'+'b'*64
    monkeypatch.setattr(images, '_docker', inspect)
    with (pytest.raises(ValueError, match=r'local.*identity'),
          images.candidate_image('sha256:'+'a'*64, tmp_path/'image', allow_local_image_id=True)):
        pytest.fail('mismatched local image was published')
    assert not (tmp_path/'image').exists()
