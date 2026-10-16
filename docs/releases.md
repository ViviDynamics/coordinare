# Installing release packages

Each GitHub release contains the daemon and optional performer wheels, the shared
package wheels they require, source distributions, and
`SHA256SUMS`. An `install.sh` asset is included when that script exists in the
tagged source tree.

Install the daemon from a wheel URL on the GitHub release with `uv tool install
<wheel URL>`. Its shared dependencies use direct URLs to wheels from the same
GitHub release. PyPI and mirrors that serve only PyPI packages cannot resolve
these packages. Deleting a release breaks installs of that version.

The GitHub release and the `latest` container images are separate tracks. The
Python release stays a draft until its build, locked dependency smoke tests,
attestation, checksums, and asset upload succeed. A failed attempt leaves the
draft available for reuse by a workflow rerun. Image promotion retains its own
image build dependencies, so a Python packaging failure does not block the
`latest` image tags. The two tracks can temporarily advertise different versions.

The build job writes `SHA256SUMS` after both smoke tests and transfers it with
the artifacts. The publication job verifies those checksums and the exact file
inventory before GitHub attestation. It uploads only the verified files and that
same manifest. Provenance is recorded by GitHub attestation rather than uploaded
as a separate release asset. When reusing a draft, publication first deletes
existing assets whose names match the new uploads. A checksum mismatch, missing
or extra file, failed deletion, or failed upload leaves the release unpublished.

Smoke tests install external dependencies exported with hashes from `uv.lock`.
The optional performer installation also uses its own lock file. Local release
wheels are installed without dependency resolution while their GitHub draft URLs
are private, and dependency compatibility is checked after installation.

Legacy tags such as `2026.09.01.3` remain valid download paths. Package metadata,
wheel filenames, and runtime versions normalize numeric components to
`2026.9.1.3`, as required by Python version normalization. New tags retain their
existing version spelling.
