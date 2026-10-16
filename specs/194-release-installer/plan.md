# Implementation plan

Use stock curl, awk, sed, mktemp, uname, and sha256sum or shasum. Download to a
private temporary directory and clean it on exit. Parse the manifest without
executing its contents, reject malformed hashes, duplicate names and unsafe paths,
then compare each required wheel's digest. Keep release download overrides usable
with file URLs for tests. Keep persistent uv storage inside HOME.

Write subprocess behavior tests first, observe failures, then implement install.sh.
Add release CI smoke checks on Ubuntu and macOS and document usage. Run lint,
type checks, the baseline ratchet, related pytest selections and shellcheck.
