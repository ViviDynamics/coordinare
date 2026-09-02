# Coordinare in the Cluster Cluster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run coordinare in the cluster Kubernetes cluster, dispatching real cards to performer Pods whose environment cache lives on a storage-server NFS claim, with ArgoCD keeping the deployment in sync with this repository.

**Architecture:** Coordinare's CI publishes its Helm chart as an OCI artifact to `ghcr.io/vividynamics/charts`; two ArgoCD Applications in the infrastructure repo track it — one owning the namespace and the cache PVC, one owning the chart. The Kubernetes pod builder learns to honour the per-symphony, read-write-for-bootstrap-only cache mounts that the Docker path already produces, which is what makes a shared NFS cache safe. Before any of it reaches cluster, the pod-level end-to-end test that no workflow currently runs gets wired into CI.

**Tech Stack:** Python 3.12+ (coordinare, `kubernetes` client), Helm 3.16, ArgoCD, kind (CI), GitHub Actions, NFS-backed `nfs-storage-server` StorageClass.

**Spec:** `specs/159-cluster-deployment/spec.md`

## Global Constraints

- Coordinare supports **exactly one replica**. The chart already refuses more; nothing here may add a second.
- Performer Pods run AI-generated code: `automountServiceAccountToken: false` stays, and no Docker socket is mounted anywhere.
- **No credential value and no model-host IP address** may appear in any committed file, in either repository (spec FR-009, FR-010, SC-006).
- Every existing Docker-path test must pass unmodified (spec SC-005).
- Run tests with `.venv/bin/pytest` — not `python -m pytest` (pyenv shim issues). Lint with `.venv/bin/ruff check src tests`.
- Releases and chart versions are **one CalVer**, matching other in-house projects: `YYYY.M.<counter>`, month
  unpadded, counter incrementing within the month. It is valid SemVer, so no mapping exists anywhere.
- Branch discipline: work on `159-cluster-deployment` in coordinare, and a matching branch in `infrastructure`. Never push to `main`.

---

### Task 1: The pod builder honours the cache mounts coordinare already computed

**Files:**
- Modify: `src/coordinare/services/kubernetes_runtime.py:168-172`
- Test: `tests/unit/test_146_kubernetes_transport.py`

**Interfaces:**
- Consumes: `PerformerEndpointConfig.volumes: list[VolumeMount]` and `.container_devenv_root: str` (`src/coordinare/models/performer_endpoint.py:56,96,100`). `VolumeMount` is `{host_path: Path, container_path: PurePosixPath, mode: Literal["ro","rw"]}`.
- Produces: `build_pod_manifest(...)` unchanged in signature; its output now contains one `volumeMounts` entry per devenv `VolumeMount`, each with `subPath` and `readOnly`.

**Background the implementer needs.** `get_env_volume_for_symphony` (`src/coordinare/services/env_cache.py:132`) already decides the hard part: it returns `mode="rw"` for a bootstrap dispatch and `mode="ro"` for every consumer, with `container_path = f"{devenv_root}/{state.sanitised_name}"` and `host_path = state.cache_dir`. The Docker path uses that verbatim. The Kubernetes builder ignores it and mounts the whole claim read-write at `/devenv` for everyone, which on one shared NFS volume is several writers in one tree. The fix is a translation, not a new policy: the subpath within the PVC is the cache directory's own name (`host_path.name`, which equals `sanitised_name`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_146_kubernetes_transport.py  (append)

class TestTheCacheMountMirrorsTheDockerPath:
    """Spec 159 US2 — one writer per symphony, enforced by the mount.

    The Docker path gives read-write to the bootstrap performer and read-only to
    consumers. This builder mounted one claim read-write for everybody, which is
    safe on a local disk with one performer and unsafe on a shared NFS claim with
    several.
    """

    @staticmethod
    def _config(volumes):
        from coordinare.models.performer_endpoint import PerformerEndpointConfig

        return PerformerEndpointConfig(
            id="perf-1", roles=["implementer"], image="coordinare-performer:full",
            port=8080, volumes=volumes,
        )

    @staticmethod
    def _mount(sanitised, mode):
        from coordinare.models.performer_endpoint import VolumeMount

        return VolumeMount(
            host_path=f"/var/lib/coordinare/devenv/{sanitised}",
            container_path=f"/devenv/{sanitised}",
            mode=mode,
        )

    def _built(self, volumes, cache_claim="coordinare-devenv-cache"):
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        return build_pod_manifest(
            self._config(volumes), pod_name="p", performer_id="perf-1",
            cache_claim=cache_claim,
        )

    def test_a_bootstrap_mount_is_read_write_at_its_own_subpath(self) -> None:
        manifest = self._built([self._mount("website", "rw")])
        mounts = manifest["spec"]["containers"][0]["volumeMounts"]

        assert len(mounts) == 1
        assert mounts[0]["mountPath"] == "/devenv/website"
        assert mounts[0]["subPath"] == "website"
        assert not mounts[0].get("readOnly", False)

    def test_a_consumer_mount_is_read_only(self) -> None:
        mounts = self._built([self._mount("website", "ro")])["spec"]["containers"][0]["volumeMounts"]

        assert mounts[0]["readOnly"] is True, (
            "a consumer holding a read-write mount is how two performers end up "
            "writing one cache tree on NFS"
        )

    def test_one_symphony_cannot_reach_another_through_its_mount(self) -> None:
        mounts = self._built([self._mount("website", "ro")])["spec"]["containers"][0]["volumeMounts"]

        assert mounts[0]["subPath"] == "website"
        assert all(m["mountPath"] != "/devenv" for m in mounts), (
            "mounting the claim root exposes every symphony's cache to every performer"
        )

    def test_several_symphonies_each_get_their_own_subpath(self) -> None:
        mounts = self._built(
            [self._mount("website", "ro"), self._mount("inhouse", "ro")]
        )["spec"]["containers"][0]["volumeMounts"]

        assert {m["subPath"] for m in mounts} == {"website", "inhouse"}
        assert len({m["name"] for m in mounts}) == 1, "one claim, several subpaths"

    def test_the_claim_is_declared_once_however_many_mounts(self) -> None:
        manifest = self._built([self._mount("website", "ro"), self._mount("inhouse", "ro")])
        volumes = manifest["spec"]["volumes"]

        assert len(volumes) == 1
        assert volumes[0]["persistentVolumeClaim"]["claimName"] == "coordinare-devenv-cache"

    def test_no_claim_means_no_mounts_and_no_failure(self) -> None:
        """Spec 146 FR-012: a cluster with no cache runs cold, not broken."""
        manifest = self._built([self._mount("website", "ro")], cache_claim=None)

        assert "volumeMounts" not in manifest["spec"]["containers"][0]
        assert "volumes" not in manifest["spec"]

    def test_a_claim_with_no_volumes_mounts_nothing(self) -> None:
        """A performer with no env cache yet must not get the claim root."""
        manifest = self._built([])

        assert "volumeMounts" not in manifest["spec"]["containers"][0]
        assert "volumes" not in manifest["spec"]

    def test_a_workspace_volume_never_reaches_the_nfs_claim(self) -> None:
        """Spec FR-008. Git trees and node_modules on NFS is the worst case for
        many-small-files and locking, and the claim is not where they belong.
        Only devenv paths translate; anything else is ignored here."""
        from coordinare.models.performer_endpoint import VolumeMount

        workspace = VolumeMount(
            host_path="/var/lib/coordinare/work/card-1",
            container_path="/workspace",
            mode="rw",
        )
        manifest = self._built([workspace, self._mount("website", "ro")])
        mounts = manifest["spec"]["containers"][0]["volumeMounts"]

        assert [m["mountPath"] for m in mounts] == ["/devenv/website"], (
            "a non-devenv volume was mounted onto the shared NFS claim"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_146_kubernetes_transport.py -k TestTheCacheMountMirrorsTheDockerPath -v`

Expected: FAIL. The first assertion to blow up is `mounts[0]["mountPath"] == "/devenv/website"` — today it is `/devenv` — and `test_a_claim_with_no_volumes_mounts_nothing` fails because the claim is mounted whenever `cache_claim` is set.

- [ ] **Step 3: Replace the cache-mount block**

In `src/coordinare/services/kubernetes_runtime.py`, replace lines 168-172:

```python
    if cache_claim:
        container["volumeMounts"] = [{"name": "devenv-cache", "mountPath": "/devenv"}]
        spec["volumes"] = [
            {"name": "devenv-cache", "persistentVolumeClaim": {"claimName": cache_claim}}
        ]
```

with:

```python
    # 159: mirror the Docker path rather than inventing a policy here.
    # get_env_volume_for_symphony has already decided which symphony this
    # performer may see and whether it may write: rw for the bootstrap dispatch
    # that BUILDS the cache, ro for every consumer that reads it. Mounting the
    # claim root read-write for everybody was safe on a local disk with one
    # performer and is not on a shared NFS claim, where several performers can
    # write one tree at once. The subpath within the claim is the cache
    # directory's own name, which is env_cache's sanitised symphony name.
    devenv_root = config.container_devenv_root.rstrip("/")
    devenv_mounts = [
        volume
        for volume in (config.volumes or [])
        if str(volume.container_path).startswith(f"{devenv_root}/")
    ]
    if cache_claim and devenv_mounts:
        container["volumeMounts"] = [
            {
                "name": "devenv-cache",
                "mountPath": str(volume.container_path),
                "subPath": Path(volume.host_path).name,
                "readOnly": volume.mode == "ro",
            }
            for volume in devenv_mounts
        ]
        spec["volumes"] = [
            {"name": "devenv-cache", "persistentVolumeClaim": {"claimName": cache_claim}}
        ]
```

If `Path` is not already imported in this module, add `from pathlib import Path` to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_146_kubernetes_transport.py -v`

Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Mutation-test the two assertions that carry the safety property**

Verify each guard actually fails when broken. In the real tree (not a `/tmp` copy — the venv installs `coordinare` editable, so a copy is never imported), back up the file, break it, run, restore:

```bash
cp src/coordinare/services/kubernetes_runtime.py /tmp/kr.bak
# 1. consumers get read-write again
sed -i '' 's/"readOnly": volume.mode == "ro",/"readOnly": False,/' src/coordinare/services/kubernetes_runtime.py
.venv/bin/pytest tests/unit/test_146_kubernetes_transport.py -k read_only -q   # MUST fail
cp /tmp/kr.bak src/coordinare/services/kubernetes_runtime.py
# 2. subPath dropped, so every performer sees every cache
sed -i '' '/"subPath": Path(volume.host_path).name,/d' src/coordinare/services/kubernetes_runtime.py
.venv/bin/pytest tests/unit/test_146_kubernetes_transport.py -k subpath -q     # MUST fail
cp /tmp/kr.bak src/coordinare/services/kubernetes_runtime.py && rm /tmp/kr.bak
```

Both must report failures. If either passes, the test is decorative — fix the test before moving on.

- [ ] **Step 6: Run the full suite and lint**

Run: `.venv/bin/pytest tests/unit -q && .venv/bin/ruff check src tests`

Expected: all pass. Docker-path tests are untouched (spec SC-005).

- [ ] **Step 7: Commit**

```bash
git add src/coordinare/services/kubernetes_runtime.py tests/unit/test_146_kubernetes_transport.py
git commit -m "fix(146): the k8s cache mount ignored the rw/ro the Docker path computes (#159)

get_env_volume_for_symphony already decides which symphony a performer may see
and whether it may write: rw for the bootstrap dispatch that builds the cache, ro
for every consumer. The pod builder ignored all of it and mounted the claim root
read-write for every performer, which is safe on a local disk with one performer
and unsafe on the shared NFS claim this deployment uses.

Each devenv volume now becomes a subPath mount on the one claim, read-only unless
the dispatch is the bootstrap. A performer with no env cache gets no mount at all
rather than the claim root."
```

---

### Task 2: Run the pod-level end-to-end test in CI

**Files:**
- Modify: `.github/workflows/pr-ci.yml:140-181` (the `chart-install` job)
- Read: `tests/integration/test_146_kind_e2e.py`

**Interfaces:**
- Consumes: the kind cluster the `Chart Install (kind)` job already creates (`cluster_name: coordinare-ci`), and the performer image built from `Dockerfile.base`.
- Produces: nothing importable. A CI job that fails when a performer Pod cannot start, serve and stop.

**Background.** `tests/integration/test_146_kind_e2e.py` starts a real performer Pod, serves `/status`, stops it, sweeps orphans, checks an unpullable image fails fast, and verifies the shipped RBAC in both directions. No workflow runs it. It skips — visibly, with the `kind create cluster` command in the skip reason — when no cluster is reachable, so adding it to a job that already has one is nearly free. Its own docstring names the risk this closes: *"A silently-skipped integration test is how 'it works on Kubernetes' remains green while quietly becoming untrue."* It expects the image `coordinare-performer:base` loaded into the cluster.

- [ ] **Step 1: Add the image load and the test run to the existing kind job**

In `.github/workflows/pr-ci.yml`, in the `chart-install` job, after the existing "Build and load the daemon image" step, add:

```yaml
      # 159: the pod-level e2e needs the performer image in the cluster. Built
      # from Dockerfile.base rather than :full — the test starts a Pod and talks
      # to its /status, which base serves, and full adds several minutes of
      # toolchain to every PR for nothing this test asserts.
      - name: Build and load the performer image
        run: |
          docker build -t coordinare-performer:base -f Dockerfile.base .
          kind load docker-image coordinare-performer:base --name coordinare-ci
```

and after the existing "Install the chart and verify it" step, add:

```yaml
      # Spec 146's SC-001 lives here. Without this the Kubernetes performer path
      # is exercised by nothing that runs on its own.
      - name: Verify performers run as Pods
        run: uv run pytest tests/integration/test_146_kind_e2e.py -v
```

- [ ] **Step 2: Prove the test does not silently skip**

The whole point is that a skip looks like a pass. Add a guard step immediately after it:

```yaml
      - name: Fail if the pod e2e skipped
        run: |
          uv run pytest tests/integration/test_146_kind_e2e.py -q -rs 2>&1 | tee /tmp/e2e.txt
          if grep -qiE "^SKIPPED|[0-9]+ skipped" /tmp/e2e.txt; then
            echo "::error::the Kubernetes pod e2e skipped in CI; it proves nothing here"
            exit 1
          fi
```

- [ ] **Step 3: Verify locally first, against the kind cluster that already exists**

A `coordinare-dev` kind cluster is already running (k8s v1.37.0). Use it rather than waiting on CI:

```bash
docker build -t coordinare-performer:base -f Dockerfile.base .
kind load docker-image coordinare-performer:base --name coordinare-dev
kubectl config use-context kind-coordinare-dev
.venv/bin/pytest tests/integration/test_146_kind_e2e.py -v
```

Expected: PASS, and **not** "skipped". If it skips, the cluster is unreachable or the context is wrong — fix that before trusting CI. If it fails, that is spec 146's SC-001 failing for the first time in a place anyone can see, which is the point of this task; fix the cause before continuing.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/pr-ci.yml
git commit -m "ci: run the Kubernetes pod e2e that nothing was running (#159)

test_146_kind_e2e starts a real performer Pod, serves, stops, sweeps orphans and
checks the shipped RBAC both ways -- and no workflow ran it, so spec 146's SC-001
rested on a hand-run spike. The chart-install job already builds a kind cluster,
so this costs one image load.

It skips when no cluster is reachable, which in CI would look exactly like a pass,
so the run is followed by a step that fails the job if anything skipped."
```

---

### Task 3: One CalVer for coordinare, matching other in-house projects, stamped by one job

**Files:**
- Create: `.github/scripts/next_calver.py`
- Create: `tests/unit/test_159_calver.py`
- Modify: `.github/workflows/main-branch-build.yml:51-80` (the `derive-version` step) and the publish that follows

**Interfaces:**
- Consumes: the existing git tags, via `git tag -l`.
- Produces: `next_calver(tags: Iterable[str], *, year: int, month: int) -> str` returning `YYYY.M.N`; the same string is the git tag, the GitHub release, the image tag, and the chart version.

**Background the implementer needs — read this before touching the workflow.**

Coordinare mints `YYYY.MM.DD[.N]` (`2026.09.01.3`). That is four components with a zero-padded
month, so it is not valid SemVer and Helm will not take it as a chart version. in-house hit exactly
this and fixed it, and its comments say how: `YYYY.M.patch`, minted once, used for everything.

> *"THE CHART RIDES THE SAME CALVER, STAMPED BY THE SAME JOB (#2700). The publish used to live in
> helm-chart.yml computing `0.1.<commit-count>` — a second workflow deriving a second version on
> the same push. Moving it HERE, after the mint, is the whole fix: one job computes the version
> once and stamps the tag, the GitHub release, and the chart."*

Two consequences for this plan. The chart-version mapping an earlier draft proposed is **deleted**
— there is nothing to map, because the release version is already SemVer. And the publish belongs
in the job that mints the version, not in a job downstream of it.

Three subtleties, all of which in-house learned the hard way:

1. **The month is unpadded** (`date -u +%-m`). `2026.09.0` has a leading zero and is not SemVer.
2. **The counter is per-month, not per-day.** It starts at 0 and increments; the day is not in the
   version at all.
3. **Coordinare's existing tags must not be read as candidates.** `2026.09.01.3` has four
   components; parsed loosely it yields patch `01`, and the next mint would collide with history.
   Only strict `YYYY.M.N` tags count.

`appVersion` is the release version, not the commit SHA — this is where coordinare differs from the in-house scheme
on purpose. `values.yaml` pins `image.tag` to `appVersion`, and coordinare's images are tagged with
the release version, so welding `appVersion` to the SHA would name an image tag that does not
exist.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_159_calver.py
"""159: one CalVer for the tag, the release, the image and the chart.

Matching other in-house projects. The version is `YYYY.M.N` — month unpadded, counter
per month — which is valid SemVer, so nothing anywhere maps one version onto
another. Coordinare previously minted `YYYY.MM.DD[.N]`, which is neither SemVer
nor what the other repos do.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_MODULE = Path(".github/scripts/next_calver.py")


def _load():
    spec = importlib.util.spec_from_file_location("next_calver", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_first_release_of_a_month_starts_at_zero() -> None:
    assert _load().next_calver([], year=2026, month=9) == "2026.9.0"


def test_it_continues_the_months_counter() -> None:
    tags = ["2026.9.0", "2026.9.1", "2026.9.2"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.3"


def test_it_ignores_other_months_and_years() -> None:
    tags = ["2026.8.41", "2025.9.99", "2026.10.3"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.0"


def test_the_counter_is_numeric_not_lexical() -> None:
    """`9` sorts after `10` as text, and a version that goes backwards is worse
    than one that jumps."""
    tags = ["2026.9.8", "2026.9.9", "2026.9.10"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.11"


def test_coordinares_old_four_component_tags_are_not_candidates() -> None:
    """The tags this repo already has. Parsed loosely, `2026.09.01.3` offers a
    patch of `01` and the next mint collides with history."""
    tags = ["2026.09.01", "2026.09.01.3", "2026.08.30.4", "2026.9.0"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.1"


def test_a_v_prefixed_tag_still_counts() -> None:
    """the in-house scheme mints without a `v` but accepts both, because retagging git does not
    rename artefacts that were published under the old name."""
    tags = ["v2026.9.4"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.5"


def test_junk_tags_are_ignored_rather_than_fatal() -> None:
    tags = ["latest", "release-candidate", "", "2026.9.1", "2026.9.x"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.2"


@pytest.mark.parametrize(("year", "month"), ((2026, 9), (2026, 12), (2027, 1)))
def test_every_output_is_valid_semver(year, month) -> None:
    semver = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

    assert semver.match(_load().next_calver([], year=year, month=month))


def test_the_month_is_never_zero_padded() -> None:
    """`2026.09.0` is not SemVer, and Helm refuses it."""
    assert _load().next_calver([], year=2026, month=9) == "2026.9.0"


def test_ordering_survives_a_month_rollover() -> None:
    versions = ["2026.9.0", "2026.9.10", "2026.10.0", "2027.1.0"]
    keyed = [tuple(int(p) for p in v.split(".")) for v in versions]

    assert keyed == sorted(keyed)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_159_calver.py -v`

Expected: FAIL — the module does not exist.

- [ ] **Step 3: Write the script**

```python
# .github/scripts/next_calver.py
"""The next CalVer for this repository: `YYYY.M.N`.

The same scheme in-house and sibling repos use, so every repo in the org versions the same
way and a chart version is just the release version. Month is unpadded and the
counter runs per month, which makes the result valid SemVer — Helm requires that,
and it is why nothing here maps one version onto another.

Coordinare previously minted `YYYY.MM.DD[.N]`. Those tags still exist and are
deliberately NOT candidates: parsed loosely, `2026.09.01.3` offers a patch of
`01`, and the next mint would collide with a tag that is already pushed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from datetime import datetime, timezone

# Strict: exactly three numeric components, optional legacy `v`. Anything else --
# the old four-component tags, release branches, junk -- is not a candidate.
_TAG = re.compile(r"^v?(\d{4})\.(\d{1,2})\.(\d+)$")


def next_calver(tags: Iterable[str], *, year: int, month: int) -> str:
    patches = []
    for tag in tags:
        match = _TAG.match((tag or "").strip())
        if not match:
            continue
        tag_year, tag_month, patch = (int(g) for g in match.groups())
        if tag_year == year and tag_month == month:
            patches.append(patch)
    return f"{year}.{month}.{max(patches) + 1 if patches else 0}"


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    existing = subprocess.run(
        ["git", "tag", "-l"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    print(next_calver(existing, year=now.year, month=now.month))
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_159_calver.py -v && .venv/bin/ruff check .github/scripts/next_calver.py tests/unit/test_159_calver.py`

Expected: PASS, lint clean.

- [ ] **Step 5: Check what it would mint against this repository's real tags**

```bash
git fetch --tags
python3 .github/scripts/next_calver.py
```

Expected on 2026-09-01: `2026.9.0` — the four-component tags are correctly ignored. Confirm the
value does not already exist: `git tag -l "$(python3 .github/scripts/next_calver.py)"` prints
nothing.

- [ ] **Step 6: Replace the derive step and move the publish into the same job**

In `.github/workflows/main-branch-build.yml`, replace the body of the "Derive CalVer version from
existing tags" step (currently `today="$(date -u +%Y.%m.%d)"` and its suffix logic) with:

```yaml
      - name: Derive CalVer version from existing tags
        id: derive
        shell: bash
        run: |
          set -euo pipefail
          # One scheme across the org (other in-house projects, coordinare): YYYY.M.N, month
          # unpadded, counter per month. Valid SemVer, so the chart version below
          # is this string verbatim rather than a mapping of it.
          existing="$(git tag --points-at HEAD | grep -E '^v?[0-9]{4}\.[0-9]{1,2}\.[0-9]+$' | head -n 1 || true)"
          if [ -n "$existing" ]; then
            version="${existing#v}"
          else
            version="$(python3 .github/scripts/next_calver.py)"
            git tag "$version"
            git push origin "$version"
          fi
          echo "Version: $version"
          echo "version=$version" >> "$GITHUB_OUTPUT"
```

Then add the chart publish **to this same job**, after the tag exists — this is the half in-house calls
"the whole fix", because a second job deriving its own version is how the two drift:

```yaml
      - name: Install helm
        uses: azure/setup-helm@v4
        with:
          version: v3.16.2

      - name: Log in to ghcr
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # appVersion is the release version, not the commit SHA as in-house uses:
      # values.yaml pins image.tag to appVersion, and coordinare's images are
      # tagged with the release, so a SHA here would name an image that does not
      # exist.
      - name: Publish the chart at the minted version
        shell: bash
        run: |
          set -euo pipefail
          version='${{ steps.derive.outputs.version }}'
          out="$(mktemp -d)"
          echo "== packaging coordinare ${version} =="
          helm package deploy/helm/coordinare \
            --version "${version}" --app-version "${version}" -d "${out}"
          helm push "${out}"/coordinare-*.tgz oci://ghcr.io/vividynamics/charts
```

- [ ] **Step 7: Verify the packaging locally before trusting CI**

```bash
helm package deploy/helm/coordinare --version 2026.9.0 --app-version 2026.9.0 -d /tmp/chart
helm show chart /tmp/chart/coordinare-2026.9.0.tgz | grep -E "^version|^appVersion"
```

Expected: `version: 2026.9.0`, `appVersion: 2026.9.0`. Helm rejecting the version means the scheme
is wrong — do not work around it by editing the chart. Do not push by hand; the job does that.

- [ ] **Step 8: Commit**

```bash
git add .github/scripts/next_calver.py tests/unit/test_159_calver.py .github/workflows/main-branch-build.yml
git commit -m "ci: one CalVer for coordinare, matching other in-house projects (#159)

Coordinare minted YYYY.MM.DD[.N] -- four components, zero-padded month -- which is
not SemVer, so a chart version had to be mapped out of it. in-house already solved
this: YYYY.M.N, minted once, used as the tag, the release, the image tag and the
chart version. Adopting it deletes the mapping rather than testing it.

The publish moves into the job that mints the version, which is the part the in-house scheme
calls the whole fix: a second workflow deriving its own version on the same push
is how the two drift apart.

The existing four-component tags are deliberately not candidates for the counter.
Parsed loosely, 2026.09.01.3 offers a patch of 01, and the next mint would
collide with a tag that is already pushed."
```

---

### Task 4: The namespace, the cache claim, and the secrets runbook

**Repository: `infrastructure`** (branch: `coordinare-cluster`)

**Files:**
- Create: `clusters/cluster/namespaces/coordinare/namespace.yaml`
- Create: `clusters/cluster/namespaces/coordinare/pvc-devenv-cache.yaml`
- Create: `clusters/cluster/namespaces/coordinare/README.md`

**Interfaces:**
- Consumes: the `nfs-storage-server` StorageClass (SSD-backed ZFS on `192.168.3.16`, RWX-capable).
- Produces: namespace `coordinare`; PVC `coordinare-devenv-cache`, referenced by Task 5's Application as `performers.cacheClaim`.

- [ ] **Step 1: Write the namespace**

```yaml
# clusters/cluster/namespaces/coordinare/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: coordinare
```

- [ ] **Step 2: Write the cache claim**

```yaml
# clusters/cluster/namespaces/coordinare/pvc-devenv-cache.yaml
#
# The environment cache performer Pods share. ReadWriteMany because several
# performers mount it at once -- one per symphony writes (the bootstrap
# dispatch), the rest read. Coordinare enforces that split in the mount itself:
# consumers get readOnly, and each symphony is confined to its own subPath.
#
# nfs-storage-server is the SSD tier (10GbE, single NFS hop to ZFS) and the
# fastest in the cluster's own benchmarks. It is also RWX, which ceph-rbd is not.
#
# Owned by the manifests Application, NOT the chart, so a chart uninstall cannot
# take a populated cache with it.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: coordinare-devenv-cache
  namespace: coordinare
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: nfs-storage-server
  resources:
    requests:
      # Toolchains, not artefacts: a language runtime plus its package cache per
      # symphony. Generous rather than tight, because a bootstrap that runs out
      # of room fails in a way that looks like a broken environment.
      storage: 100Gi
```

- [ ] **Step 3: Write the README, including the preflight that catches the likeliest failure**

```markdown
# coordinare (cluster)

The coordinare daemon, managing performer Pods. Chart published by the coordinare
repo to `ghcr.io/vividynamics/charts`; see `../argocd/application-coordinare.yaml`.

## Secrets (applied out-of-band, never committed)

```sh
# GitHub credential the daemon uses for the board, PRs and clones.
kubectl -n coordinare create secret generic coordinare \
  --from-literal=GITHUB_TOKEN='...' \
  --from-literal=LITELLM_MASTER_KEY='...'

# ghcr pull secret for the daemon and performer images (private packages);
# a PAT with read:packages.
kubectl -n coordinare create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username='<github-user>' \
  --docker-password='<PAT with read:packages>'
```

## Before the first dispatch: prove the cache is writable

The likeliest thing to be wrong on first contact. The chart runs as UID 10001
with `fsGroup: 10001`, and NFS exports commonly squash or ignore supplemental
groups — in which case the bootstrap performer cannot populate the cache, and the
symptom looks like a broken environment rather than a permissions problem.

```sh
kubectl -n coordinare run nfs-check --rm -it --restart=Never \
  --image=busybox --overrides='
{"spec":{"securityContext":{"runAsUser":10001,"fsGroup":10001},
 "containers":[{"name":"c","image":"busybox","command":["sh","-c",
 "touch /devenv/.writetest && echo WRITABLE && rm /devenv/.writetest"],
 "volumeMounts":[{"name":"cache","mountPath":"/devenv"}]}],
 "volumes":[{"name":"cache","persistentVolumeClaim":{"claimName":"coordinare-devenv-cache"}}]}}'
```

Expect `WRITABLE`. If it fails, fix the export (`storage_setup` ansible role,
`nfs_exports`) before deploying — not after a card has failed.

## The dashboard has no authentication

Spec 143 will add it. Until then the Service is ClusterIP with no Ingress and is
reached by port-forward:

```sh
kubectl -n coordinare port-forward svc/coordinare 8090:8090
```

Do not put it behind the tunnel.
```

- [ ] **Step 4: Validate the manifests render and apply cleanly**

```bash
kubectl apply --dry-run=server -f clusters/cluster/namespaces/coordinare/
```

Expected: namespace and PVC both validate. `--dry-run=server` (not `client`) so the StorageClass name is checked against the cluster.

- [ ] **Step 5: Commit**

```bash
git add clusters/cluster/namespaces/coordinare
git commit -m "coordinare: namespace and the shared env-cache claim

RWX on nfs-storage-server: several performers mount it at once, one writing per
symphony and the rest reading, with coordinare enforcing that in the mount itself.
Owned here rather than by the chart so a chart uninstall cannot take a populated
cache with it.

The README leads with the NFS write check, because UID 10001 against an export
that squashes groups is the likeliest first-contact failure and it presents as a
broken environment rather than a permissions error."
```

---

### Task 5: The ArgoCD Applications

**Repository: `infrastructure`** (branch: `coordinare-cluster`)

**Files:**
- Create: `clusters/cluster/namespaces/argocd/application-coordinare-manifests.yaml`
- Create: `clusters/cluster/namespaces/argocd/application-coordinare.yaml`

**Interfaces:**
- Consumes: `ghcr.io/vividynamics/charts/coordinare` (Task 3), PVC `coordinare-devenv-cache` (Task 4), the in-cluster LiteLLM Service, and the out-of-band `coordinare` and `ghcr-pull` Secrets.
- Produces: a running deployment that reconciles on every chart release.

**Background.** Two Applications, as other in-house projects do: `application-infra-manifests.yaml` owns the namespace, `application-infra.yaml` owns the chart. Keeping storage out of the chart Application means a chart-level prune or uninstall cannot delete a populated cache.

- [ ] **Step 1: Write the manifests Application**

```yaml
# clusters/cluster/namespaces/argocd/application-coordinare-manifests.yaml
#
# The namespace and the env-cache PVC. Separate from the chart Application so
# that storage outlives any chart operation.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: coordinare-manifests
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: platform

  destination:
    server: https://kubernetes.default.svc
    namespace: coordinare

  source:
    repoURL: https://github.com/ViviDynamics/infrastructure.git
    targetRevision: main
    path: clusters/cluster/namespaces/coordinare

  syncPolicy:
    automated:
      # Never prune here. This Application owns the claim holding every
      # symphony's built toolchain; a prune would discard hours of bootstrap.
      prune: false
      selfHeal: true
```

- [ ] **Step 2: Write the chart Application**

```yaml
# clusters/cluster/namespaces/argocd/application-coordinare.yaml
#
# Coordinare, tracked from the OCI chart its repo publishes (the publish-chart job
# added in coordinare #159, modeled on other in-house projects').
#
# Secrets are applied out-of-band; see ../coordinare/README.md. No prune: the
# out-of-band Secrets stay untouched.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: coordinare
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: platform

  destination:
    server: https://kubernetes.default.svc
    namespace: coordinare

  source:
    repoURL: ghcr.io/vividynamics/charts
    chart: coordinare
    # Chart versions are the release CalVer verbatim (YYYY.M.N), the same scheme
    # in-house and sibling repos use — ordinary monotonic SemVer, so the range takes the
    # newest release and appVersion welds the daemon image to it. The range
    # starts at 0.1.0 so the chart's pre-CalVer version is covered too; the
    # cutover is monotonic because 2026.x.x > 0.1.x. A merge to coordinare's main
    # deploys itself.
    targetRevision: ">=0.1.0"
    helm:
      releaseName: coordinare
      valuesObject:
        imagePullSecrets:
          - name: ghcr-pull

        existingSecret: coordinare

        state:
          storageClass: nfs-storage-server
          size: 8Gi

        performers:
          namespace: coordinare
          cacheClaim: coordinare-devenv-cache
          imagePullSecrets:
            - name: ghcr-pull
          # Left off deliberately. The chart's own values say it: a NetworkPolicy
          # the CNI does not enforce gives no protection while looking as though
          # it does. Turn it on only after `coordinare doctor --check egress`
          # proves this cluster enforces egress.
          egress:
            enabled: false

        # ClusterIP, no Ingress: the dashboard has no authentication until spec
        # 143. Reached by port-forward. See ../coordinare/README.md.
        dashboard:
          service:
            type: ClusterIP

        config:
          # By Service name, never by address: the model host's IP has moved
          # under this system before, and LiteLLM owns that route.
          endpoints:
            - name: litellm
              kind: openai
              base_url: http://litellm.litellm.svc.cluster.local:4000/v1
              api_key: ${LITELLM_MASTER_KEY}

  syncPolicy:
    automated:
      prune: false
      selfHeal: true
    syncOptions:
      - CreateNamespace=false # the manifests Application owns the namespace
```

- [ ] **Step 3: Confirm the LiteLLM Service name and port before applying**

The `base_url` above is an assumption. Check it:

```bash
kubectl -n litellm get svc
```

Use the actual Service name and port. If it differs, correct `base_url` in the file — do not leave the guess in.

- [ ] **Step 4: Validate**

```bash
kubectl apply --dry-run=client -f clusters/cluster/namespaces/argocd/application-coordinare-manifests.yaml
kubectl apply --dry-run=client -f clusters/cluster/namespaces/argocd/application-coordinare.yaml
```

Expected: both validate.

- [ ] **Step 5: Prove nothing secret or address-shaped got committed**

Spec SC-006. Run in the infrastructure repo, over what this branch adds:

```bash
git diff main --unified=0 -- clusters/cluster/namespaces/coordinare \
    clusters/cluster/namespaces/argocd/application-coordinare*.yaml \
  | grep -E "^\+" \
  | grep -nEi "192\.168\.|10\.[0-9]+\.[0-9]+\.[0-9]+|ghp_|sk-|BEGIN [A-Z ]*PRIVATE KEY|password:|api_key: [^$]" \
  || echo "clean"
```

Expected: `clean`. The one permitted near-miss is `api_key: ${LITELLM_MASTER_KEY}`, which is an
env-var *name* — the grep excludes it by requiring a non-`$` first character. Anything else it
prints is either a credential or a model-host address, and both are the thing this spec forbids.

- [ ] **Step 6: Commit**

```bash
git add clusters/cluster/namespaces/argocd/application-coordinare-manifests.yaml \
        clusters/cluster/namespaces/argocd/application-coordinare.yaml
git commit -m "coordinare: track the OCI chart, with storage owned separately

Same shape as other in-house projects: one Application for the namespace and the cache claim,
one for the chart, so a chart prune or uninstall cannot take a populated cache.

Models are reached by Service name rather than address -- the model host's IP has
moved under this system before, and LiteLLM owns that route. Egress policy stays
off until the CNI is proven to enforce it, and the dashboard stays ClusterIP
because it has no authentication yet."
```

---

### Task 6: First bring-up

**Files:** none. This task produces a running deployment and a recorded result.

**Interfaces:**
- Consumes: everything above.
- Produces: spec SC-001 — a card completing on a performer Pod in cluster.

- [ ] **Step 1: Create the secrets and prove the cache is writable**

Follow `clusters/cluster/namespaces/coordinare/README.md`: create both Secrets, then run the `nfs-check` Pod. **Do not continue until it prints `WRITABLE`.** A failure here is an NFS export fix, not a coordinare problem, and finding it now costs minutes instead of a failed card.

- [ ] **Step 2: Sync the manifests Application, then the chart**

```bash
kubectl -n argocd get applications coordinare-manifests coordinare
kubectl -n coordinare get pvc coordinare-devenv-cache      # expect Bound
kubectl -n coordinare get pods -w
```

Expected: the claim binds, the StatefulSet's Pod reaches Ready, and `/ready` answers. If the Pod is Pending on the claim, the StorageClass is wrong or the NFS provisioner is unhealthy.

- [ ] **Step 3: Confirm the daemon loaded its config and expanded its credentials**

```bash
kubectl -n coordinare logs sts/coordinare | grep -E "config_loaded|config_validation_error"
kubectl -n coordinare logs sts/coordinare | grep -c "'GITHUB_TOKEN'"
```

Expected: `config_loaded` present, no `config_validation_error`, and zero occurrences of the literal placeholder — the same assertions the kind test makes, now against the real cluster.

- [ ] **Step 4: Probe the model path from inside the cluster**

```bash
kubectl -n coordinare exec sts/coordinare -- \
  python3 -c "import urllib.request,json,os; \
req=urllib.request.Request('http://litellm.litellm.svc.cluster.local:4000/v1/models', \
headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}); \
print(len(json.load(urllib.request.urlopen(req,timeout=30))['data']),'models')"
```

Expected: a model count. This separates "coordinare cannot reach LiteLLM" from "the model behaved badly" before either can be confused for the other.

- [ ] **Step 5: Dispatch one card on the scratch board**

Move a single card into the dispatch column and watch both sides:

```bash
kubectl -n coordinare get pods -w
kubectl -n coordinare logs sts/coordinare -f
```

Expected: a performer Pod appears carrying coordinare's management labels, becomes Ready, does the work, and is deleted on release. Then confirm the mount discipline on a live Pod while one is running:

```bash
kubectl -n coordinare get pod -l app.kubernetes.io/managed-by=coordinare \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].volumeMounts}{"\n"}{end}'
```

Expected: a bootstrap performer shows `readOnly` absent or false with its symphony's `subPath`; every consumer shows `readOnly: true`. This is spec SC-003 observed in production rather than in a unit test.

- [ ] **Step 6: Prove the sync loop, which is the point of using ArgoCD at all**

Spec SC-004. Merge a deliberately trivial chart change to coordinare's main — a comment in
`values.yaml` is enough — and watch it arrive without running helm:

```bash
# after the merge, CI publishes; then:
kubectl -n argocd get application coordinare -o jsonpath='{.status.sync.revision}{"\n"}'
helm show chart oci://ghcr.io/vividynamics/charts/coordinare | grep -E "^version|^appVersion"
```

Expected: the Application's revision moves to the newly published chart version on its own. If it
does not, check the `targetRevision` range against the version actually minted — a range that
excludes real versions is the failure mode here, and it is silent.

- [ ] **Step 7: Record the result on the spec**

Append a short "First bring-up" section to `specs/159-cluster-deployment/spec.md` in the coordinare repo: what worked, what did not, and the actual values that differed from this plan's assumptions (LiteLLM Service name, NFS behaviour under `fsGroup`, cache size after the first bootstrap). Commit it. The next person deploying this reads that section before they read anything else.

---

## Deferred

**Issue #244 — reasoning models returning empty content** (spec US4 / FR-012) is deliberately not a task here. The chosen default `spark/qwen3.8:27b` was measured clean 4/4 on reviewer and qa contracts at 800 tokens, so SC-001 does not depend on it, and it lands on its own branch against its own issue. If a card in cluster fails with `malformed_output` under a self-hosted model, check `finish_reason` and `reasoning_content` **before** concluding the model is bad — that is precisely the trap #244 documents.
