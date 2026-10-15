# 193: Installable release artifacts

Every release extends the existing CalVer, image and chart workflow with Python
artifacts. The release tag is the package version after normalizing legacy
leading zero numeric components. Download URLs retain the original tag. The daemon wheel and sdist,
performer wheel and shared library wheels carry the repository LICENSE and NOTICE.
The three shared libraries are not assumed to exist on PyPI: release metadata pins
them to wheel URLs on the same release. Hatch reads the development version from
`_version.py`; the lock records dynamic metadata and retains all dependency pins.

A clean virtual environment must run `coordinare --version`, load the dashboard app
and all referenced assets, render service Jinja templates, read example configs,
and load default personas without the checkout. Doctor and dashboard show the
installed package version. This check uses ASGI requests without a network socket.

Release creation creates a real git ref plus a draft, explicitly not latest.
Build, install verification, optional root `install.sh`, provenance generation,
checksums and asset upload must all succeed before publication. The GitHub release
and latest image tags are separate tracks. Image promotion depends only on its
existing image jobs, so Python packaging failures do not block it. A failed upload
leaves a draft for a safe rerun. Existing published releases are detected and their
asset upload and final publication are left intact on reruns.
SHA256SUMS covers every downloadable asset except itself, including the provenance
bundle. Provenance covers the built packages and optional installer; the bundle
cannot attest itself.

Build and smoke jobs have read access only and do not persist checkout credentials.
A separate job downloads the built assets for attestation and release upload with
write permissions. Draft discovery lists releases with pagination and matches the
tag, and creation passes the release id through job outputs to final publication.

The clean environment test installs hashed external dependencies exported from
the locks and installs the exact local wheels without dependency resolution,
because draft assets are not public yet. Public release URLs remain in the
distributed metadata. Only the staged build metadata permits direct references.
No installer implementation is added for issue 194.

Acceptance: workflow contract and failure tests, actual tagged package builds,
clean installed wheel smoke check and existing license gate. GitHub execution and
browser checks remain operator validation; this worktree cannot run Actions,
bind sockets or commit.
