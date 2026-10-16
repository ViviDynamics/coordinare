"""Release creation must wait for installable images without blocking their tag."""

from __future__ import annotations

from graphlib import TopologicalSorter
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/main-branch-build.yml"
IMAGES = {"manifest-base", "manifest-full", "build-daemon"}


def workflow_jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def dependencies(job: dict) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def test_release_creation_waits_for_all_images_with_no_dependency_cycle() -> None:
    jobs = workflow_jobs()
    graph = {name: dependencies(job) for name, job in jobs.items()}
    assert all(needs <= jobs.keys() for needs in graph.values())
    order = list(TopologicalSorter(graph).static_order())
    create = jobs["create-release"]
    assert dependencies(create) == {"create-tag", *IMAGES}
    assert "always()" not in str(create)
    assert "if" not in create  # Default success() prevents creation on image failure.
    assert "github.rest.repos.createRelease" in str(create)
    assert order.index("create-tag") < min(order.index(image) for image in IMAGES)
    assert max(order.index(image) for image in IMAGES) < order.index("create-release")
    assert order.index("create-release") < order.index("publish-release")


def test_tag_creation_is_independent_of_release_objects_and_keeps_validated_sha() -> None:
    jobs = workflow_jobs()
    tag = jobs["create-tag"]
    assert dependencies(tag) == {"derive-version", "lint", "test", "coverage"}
    script = tag["steps"][-1]["with"]["script"]
    assert "github.rest.git.createRef" in script
    assert "refs/tags/${version}" in script
    assert "tagged !== sha" in script
    assert "Release" not in script
    for name in ("build-base-amd64", "build-base-arm64", "build-full-amd64", "build-full-arm64", "build-daemon"):
        job = jobs[name]
        assert "create-tag" in dependencies(job)
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["ref"] == "${{ needs.create-tag.outputs.version }}"


@pytest.mark.parametrize("name", ["release-artifacts", "installer-smoke", "publish-artifacts", "publish-release"])
def test_package_pipeline_uses_image_gated_release_metadata(name: str) -> None:
    job = workflow_jobs()[name]
    assert "create-release" in dependencies(job)
    assert job["if"] == "needs.create-release.outputs.published != 'true'"
    assert "needs.create-tag.outputs.release_id" not in str(job)
    assert "always()" not in str(job)
