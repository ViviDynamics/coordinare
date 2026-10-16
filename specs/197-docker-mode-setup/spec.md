# Docker mode setup

Extend init's containers preset to provision published performer images without a local build.

1. Probe Docker through an injectable runner. Missing CLI, stopped daemon, socket permissions,
   insufficient disk space, timeouts and pull failures give specific recovery guidance.
2. Default to the installed release version and full variant. Development builds require an
   explicit image tag. Never select latest. Offer base and full with approximate disk budgets.
3. Pull the versioned GHCR tag and inspect it. Retain the tag and pin its matching repository
   digest when Docker reports one. Do not accept unrelated repository digests.
4. Write ephemeral performer wiring, separate workspace and artifact mounts, a disjoint env
   cache root, bridge networking with a host gateway, and runtime secret delivery settings.
5. Require an API key for native providers in Docker mode because host harness login files
   are not mounted. Self-hosted endpoints without authentication remain supported.
6. Inspect instead of a costly harness install during init. Explain that host connectivity
   checks cannot prove container connectivity. Doctor warns on image version mismatch.
7. CI exercises non-interactive setup against a published image and starts a performer.

Optional daemon compose deployment is outside this change. No credentials enter images,
compose or config.yaml. Existing private environment-file handling remains authoritative.
