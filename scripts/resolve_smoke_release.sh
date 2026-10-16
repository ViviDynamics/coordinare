#!/usr/bin/env bash
set -euo pipefail

if ! tag=$(gh release list --exclude-drafts --exclude-pre-releases --limit 1 --json tagName -q '.[0].tagName'); then
  echo '::error::Could not resolve a published release for the Docker smoke job.' >&2
  exit 1
fi
if [[ ! "$tag" =~ ^[0-9]{4}\.[0-9]{1,2}\.[0-9]+(\.[0-9]+)?$ ]]; then
  echo '::error::No valid published release tag resolved for the Docker smoke job.' >&2
  exit 1
fi
echo "tag=$tag" >> "$GITHUB_OUTPUT"
