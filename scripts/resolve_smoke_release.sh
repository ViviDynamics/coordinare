#!/usr/bin/env bash
set -euo pipefail

# A release can be published before its performer images finish pushing.
# Probe both variants so the smoke never selects a partially published release.
release_limit=5
if ! tags=$(gh release list --exclude-drafts --exclude-pre-releases --limit "$release_limit" --json tagName -q '.[].tagName'); then
  echo '::error::Could not resolve a published release for the Docker smoke job.' >&2
  exit 1
fi

accept='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json'
while IFS= read -r tag; do
  if [[ ! "$tag" =~ ^[0-9]{4}\.[0-9]{1,2}\.[0-9]+(\.[0-9]+)?$ ]]; then
    echo "Skipping '${tag}': invalid published release tag."
    continue
  fi

  ready=true
  for variant in base full; do
    repository="vividynamics/coordinare-performer-${variant}"
    if ! token=$(curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
        "https://ghcr.io/token?scope=repository:${repository}:pull" |
        python3 -c '
import json, sys
token = json.load(sys.stdin).get("token")
if not isinstance(token, str) or not token:
    sys.exit(1)
print(token)
' 2>/dev/null); then
      echo "Skipping ${tag}: could not obtain anonymous GHCR token for ${variant}."
      ready=false
      break
    fi
    if ! status=$(curl --silent --show-error --connect-timeout 10 --max-time 30 \
        --head --output /dev/null --write-out '%{http_code}' \
        --header "Authorization: Bearer ${token}" --header "Accept: ${accept}" \
        "https://ghcr.io/v2/${repository}/manifests/${tag}"); then
      echo "Skipping ${tag}: ${variant} manifest request failed."
      ready=false
      break
    fi
    if [[ "$status" != 200 ]]; then
      echo "Skipping ${tag}: ${variant} manifest unavailable (HTTP ${status})."
      ready=false
      break
    fi
  done

  if [[ "$ready" == true ]]; then
    echo "Selected ${tag}: base and full performer manifests are available."
    echo "tag=$tag" >> "$GITHUB_OUTPUT"
    exit 0
  fi
done <<< "$tags"

echo "::error::No valid published release with base and full performer manifests available among the latest ${release_limit} releases for the Docker smoke job." >&2
exit 1
