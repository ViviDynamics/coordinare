# Implementation plan

Use a standard library build driver to copy only distribution inputs into a
temporary staging tree, set release versions and sibling wheel URLs, and run the
existing Hatch and setuptools backends through uv. Do not mutate checkout metadata.
Explicitly include example configuration data in both wheel and sdist. Move the
root development version to the Hatch version source and refresh lock metadata
without changing dependency versions.

Reuse the metrics package version reader for CLI, doctor and dashboard. Escape
the version before rendering it into HTML. No snapshot scope or JS changes.

Extend main branch release jobs with build and isolated installation, GitHub
provenance attestation, checksum generation and upload. Gate final publication
and image latest tagging on successful artifacts. Test the parsed workflow and
execute upload failure with a fake gh executable. Verify existing license tests.
