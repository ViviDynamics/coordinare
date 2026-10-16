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
6. Run a bounded, named and labelled throwaway container through the real entrypoint.
   Verify harness readiness and container GitHub/model connectivity, then always remove it.
   Offer --skip-smoke. Doctor warns on image version mismatch; init explains re-pinning.
7. CI resolves the newest published release at job time and exercises non-interactive
   setup with the full image. Also smoke the checkout base entrypoint and harness.
8. Preserve containers preset expansion for existing configs. Init writes new settings
   explicitly, including per-endpoint token references and a localhost publish address.
   Pass tokens through private temporary env files; support containerized daemon addresses.

Optional daemon compose deployment is outside this change. No credentials enter images,
compose or config.yaml. Existing private environment-file handling remains authoritative.
