"""A release artifact for another CPU architecture is a named correctable error; emulation is never implied by a download."""
from __future__ import annotations

import re

import pytest
from coordinare_service_inference.environment.models import Activation, Command
from coordinare_service_inference.environment.static_rules import (
    foreign_artifact,
    validate_static_rules,
)
from pydantic_core import PydanticCustomError

from coordinare.services.environment_admission import GRAPH_GUIDANCE

ARM = 'linux/aarch64'
X86 = 'linux/x86_64'


def install(id_: str, script: str) -> Command:
    return Command(id=id_, argv=['bash', '-c', script])


@pytest.mark.parametrize(('platform', 'script'), [
    (ARM, 'curl -fsSL -o /tmp/helm.tgz https://get.helm.sh/helm-v3.16.0-linux-amd64.tar.gz'),
    (ARM, 'set -e; wget -q -O /tmp/gh.tgz https://github.com/cli/cli/releases/download/v2.60.0/gh_2.60.0_linux_amd64.tar.gz'),
    (ARM, 'curl -L -o /tmp/t.tgz https://example.com/tool-linux-x86_64.tar.gz && tar -xzf /tmp/t.tgz'),
    (ARM, 'curl -o /tmp/t https://example.com/tool-linux-x64'),
    (ARM, 'gh release download v1 --pattern "tool_*_linux_amd64.tar.gz" --dir /tmp'),
    (X86, 'curl -fsSL -o /tmp/tool.tgz https://example.com/tool-linux-arm64.tar.gz'),
    # A native download beside an unconditional foreign one is still a foreign download.
    (ARM, 'curl -fsSL -o /tmp/a.tgz https://example.com/a-arm64.tgz && curl -fsSL -o /tmp/b.tgz https://example.com/b-amd64.tgz'),
    (X86, 'curl -o /tmp/tool https://example.com/tool-aarch64'),
])
def test_downloading_another_architecture_is_a_named_correctable_error(platform, script):
    with pytest.raises(PydanticCustomError) as caught:
        validate_static_rules([install('install-tool', script)], Activation(), platform)
    assert caught.value.type == 'foreign_architecture_artifact'
    assert caught.value.context == {'command_id': 'install-tool', 'unavailable': []}
    assert 'amd64' not in str(caught.value.context) and 'arm64' not in str(caught.value.context)


@pytest.mark.parametrize(('platform', 'script'), [
    (ARM, 'curl -fsSL -o /tmp/helm.tgz https://get.helm.sh/helm-v3.16.0-linux-arm64.tar.gz'),
    (ARM, 'curl -fsSL -o /tmp/tool https://example.com/tool-linux-aarch64'),
    (X86, 'curl -fsSL -o /tmp/tool.tgz https://example.com/tool-linux-amd64.tar.gz'),
    (X86, 'curl -fsSL -o /tmp/tool.tgz https://example.com/tool-linux-x86_64.tar.gz'),
    # Platform-selecting shells name both architectures; the download follows the running one.
    (ARM, 'case $(uname -m) in x86_64) A=amd64;; aarch64) A=arm64;; esac; curl -o /tmp/t https://example.com/t-$A.tgz'),
    (ARM, 'case $(uname -m) in x86_64) curl -o /tmp/t https://example.com/t-amd64.tgz;; aarch64) curl -o /tmp/t https://example.com/t-arm64.tgz;; esac'),
    # A shell variable is not a literal architecture token.
    (ARM, 'HA=arm64; curl -o /tmp/t https://example.com/t-$HA.tgz'),
    # Image platforms are not release artifacts.
    (ARM, 'docker buildx build --platform linux/amd64 -t ci-image .'),
    (ARM, 'docker image inspect --format "{{.Architecture}}" alpine:3.23 | grep amd64'),
    (ARM, 'docker pull --platform linux/amd64 debian:trixie-slim'),
    # Architecture words that no fetch command carries.
    (ARM, 'echo amd64 && uname -m'),
    (ARM, 'dpkg --add-architecture amd64 && apt-get update'),
    # A fetch that does not name an architecture.
    (ARM, 'curl -fsSL -o /tmp/t.tgz https://example.com/tool-linux.tar.gz'),
])
def test_matching_or_architecture_neutral_downloads_are_admitted(platform, script):
    validate_static_rules([install('install-tool', script)], Activation(), platform)


def test_without_a_platform_the_rule_is_not_applied():
    validate_static_rules([install('install-tool', 'curl -o /tmp/t https://example.com/t-amd64.tgz')], Activation())


def test_foreign_artifact_recognises_only_fetch_commands():
    assert foreign_artifact(install('a', 'wget https://example.com/x-amd64.deb'), ARM)
    assert not foreign_artifact(install('b', 'tar -xf /tmp/x-amd64.tgz'), ARM)


def test_guidance_names_the_rule_without_echoing_model_text():
    guidance = GRAPH_GUIDANCE['foreign_architecture_artifact']
    assert 'execution_capabilities.platform' in guidance
    assert not re.search(r'amd64|arm64|x86_64|aarch64|x64', guidance)
