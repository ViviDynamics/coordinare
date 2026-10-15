# Implementation plan

Use a standard library build driver to copy only distribution inputs into a
temporary staging tree, set release versions and sibling wheel URLs, and run the
existing Hatch and setuptools backends through uv. Do not mutate checkout metadata.
Explicitly include example configuration data in both wheel and sdist. Move the
root development version to the Hatch version source and refresh lock metadata
without changing dependency versions.

Reuse the metrics package version reader for CLI, doctor and dashboard, falling
back to the embedded build version when distribution metadata is absent. Escape
the version before rendering it into HTML. No snapshot scope or JS changes.

Extend main branch release jobs with build and isolated installation using hashed
lock exports under read permissions. Generate checksums after both smoke tests.
Transfer assets and the checksum manifest to a separate job that verifies strict
checksums and the exact inventory before GitHub provenance attestation and upload
with write permissions. Preserve the manifest unchanged. Delete matching assets
from a reused draft before uploading, and fail publication if deletion fails.
Gate final publication on successful artifacts and preserve the existing image
latest dependencies. Reuse drafts discovered through paginated release listings
and publish by the release id passed through job outputs. Test the parsed workflow,
execute the release scripts with a draft aware API mock, and execute upload failure
with a fake gh executable. Verify existing license tests and legacy tag normalization.
