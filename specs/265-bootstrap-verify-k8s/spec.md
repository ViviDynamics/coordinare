# 265: Kubernetes bootstrap verification is clean-room and authoritative

Issue #265 (P0). On Kubernetes the env bootstrap could never pass verification. The verify ran the bootstrap agent's
own `verify.sh` from the performer's `/app` directory, and the daemon's rendered scripts never reached the cache. The
first fix (commit 4935818e) carried the rendered text in the dispatch and ran it from the checkout. Review of that
commit found four blockers. This spec records the design that closes them.

## Problem (review of 4935818e)

- **B1.** The script write follows links. A planted temp link at the performer's predictable name is written and
  chmodded 755 through the link (`env_provenance.write_bootstrap_scripts`).
- **B2.** The verify text is fed to `bash -s` on stdin. A child that reads stdin swallows the rest of the script, so a
  failing verify can pass. A Gemfile evaluated by the Rails smoke can do this.
- **B3.** On Kubernetes the daemon's populated check and the freeze walk read the daemon's own `cache_dir`, which holds
  only the three seed files. `on_bootstrap_complete` downgrades a real success to failure, so the bootstrap can never
  succeed. No test covered the Kubernetes completion path end to end.
- **B4.** On Kubernetes the verdict ran in the agent's own container, with the agent's checkout as cwd and the agent's
  processes alive. That is weaker than docker's clean room.

## Design

### Local writes and the verify script (B1, B2, encodings)

- Scripts are written with a safe create. The temp name is random. It is opened with `O_CREAT | O_EXCL | O_NOFOLLOW`,
  then `fchmod`, `fsync` and `os.replace` onto the target. A link at the target is replaced, not followed.
- The verify text is never on stdin. It is written to a performer-private directory (`mkdtemp`, mode 0700, outside the
  cache and the checkout) and run as `bash <file>` with `stdin=DEVNULL`. The file is removed afterwards.
- Encoding: a text that does not encode as UTF-8 is refused with a clear reason, not a traceback. The payload bound is
  in bytes.

### Isolated verifier and walker pods on Kubernetes (B4, B3)

On a transport where the daemon cannot see the cache (`daemon_sees_performer_cache` is false), the authoritative
verdict uses two sequential pods, run after the bootstrap pod has finished and is gone. The first runs verification
only. After its deletion is confirmed by a Kubernetes 404, the second runs only the trusted provenance walker.
Removal failure is infrastructure and prevents the next pod from starting. Both mount the same claim read only
and use the same hardened security context.

- Image: daemon `kubernetes_verifier_image`, defaulting to daemon `performer_image`, never a symphony endpoint.
- Namespace: the performer namespace.
- Cache: the devenv claim, mounted read-only at the consumer mount path, with the symphony's subPath.
- No checkout, no agent, no credentials. Automount of the service account token is off. The container has no stdin.
- A real repository commit is captured at bootstrap dispatch separately from `readme_sha`, which is a synthetic
  cache fingerprint. Spec and structured rendering inputs and the verifier's project files use that commit.
- Project files the Rails smoke needs (`Gemfile`, `Gemfile.lock`, `.ruby-version`, `.tool-versions`) are fetched from
  GitHub at the bootstrap's spec SHA through `get_file_content`, and passed in. They are written to a private directory
  inside the pod, which becomes `COORDINARE_REPO_DIR`. The agent cannot have edited them, because they come from the
  commit.
- The rendered verify text is passed in the pod spec (base64 env), written privately, and run with stdin closed. The
  pod has a deadline, resource limits, and is deleted in every exit path.
- Verify result: pod phase `Succeeded` is pass, `Failed` is fail (logs tail as the reason), and an API failure or timeout
  is infrastructure (`None`, not counted as a verdict). Verify output is never parsed as provenance. A passing verify
  without a trailing newline remains a pass. Only the later walk pod can supply a provenance summary.

The bootstrap performer's own verify run stays as a fast pre-check and as feedback to the agent. It is not
authoritative on Kubernetes. `env_verify_basis` is advisory there.

### Completion on Kubernetes (B3)

- The verifier pass is the success decision. `on_bootstrap_complete` does not run the daemon-side populated check when
  the daemon cannot see the cache. A verifier pass sets readiness.
- The walk pod runs the shared trusted provenance walker from the performer image over the read only claim.
  Python uses `-P` to disable implicit cwd imports. No cache binary executes in this pod. Its stdout comes only from
  the walker, so an unterminated `printf` or forged sentinel from verification cannot hide or replace the result.
  Exactly one sentinel must be the final nonempty line; duplicated sentinels, including ones hidden midline, refuse.
  The `COORDINARE_PROVENANCE=` JSON line carries `content_digest`, `stat_digest`, `entry_count`, `total_bytes`
  and frozen activation inputs, including the deb list with SHA256 per deb. No manifest is transmitted or stored.
- Freeze integrity still covers the content present before any cache binary ran: the bootstrap writer is gone and
  both later pods mount the claim read only. Verification cannot change that content. Its private scratch directory
  is discarded before walking, so the subsequent trusted walk observes the same content without sharing processes
  or writable scratch with verification. Concurrent external writers to the claim remain outside this guarantee.
- The content digest uses SHA256 over canonical JSON lines in spec 234 walk order. Each row contains path, kind,
  mode, size, file SHA256 and link target. Inode and ctime belong only to the stat digest, so a full content pass can
  accept harmless stat churn. The stat digest uses spec 234's exact seven NUL terminated fields and encoding.
- The daemon bounds the result to 1 MiB and validates fields, digests, counters, configured walk bounds, scripts,
  activation equality and deb paths before freezing. The walker refuses entry, byte and link failures instead of
  returning a partial success. The result is consumed once after successful clean verification.
- Kubernetes stores the digests, counters, frozen time, bootstrap commit and small consumer inputs in schema 38. Old snapshots
  load with empty defaults and require a verified bootstrap before becoming ready.
- After restart, on the scheduled full pass, and after a consumer stat refusal, a walk only verifier pod rechecks
  the claim. Matching content refreshes the stat baseline. Content or input mismatch records drift. API, log read,
  timeout, pod eviction or node loss, and unexpected walk I/O failures refuse transiently and do not record drift.
  A claim `PermissionError` is a verdict failure with instructions to make files world readable and directories world
  traversable using `o+rX`. It does not loop as infrastructure.
- Every Kubernetes consumer gets the expected stat digest and walk bounds. Trusted performer code computes the
  digest on its read only mount before materialising or sourcing activation. A mismatch refuses the job and asks
  the daemon for a full content recheck. Empty or missing mounts report `env_cache_missing`.
- Both dispatch holds use persisted verifier success plus a complete frozen summary on Kubernetes. Neither walks
  the daemon seed directory or treats an unavailable local Docker verify as Kubernetes readiness.

### Wiped claims and budget

- A consumer refusal (`env_cache_missing`, from the performer) triggers a re-bootstrap through the existing
  `env_cache_health_failed` seam. The re-bootstrap must spend from the bootstrap budget (088), so a claim that is
  wiped on every bootstrap is bounded by `env_bootstrap_max_attempts`.
  Duplicate refusal signals are charged once while regeneration is requested. The persisted `bootstrap_budget_sha`
  tracks the real spec fingerprint, so a forced run's nonce key cannot re-arm the budget in flight or after restart. The repair budget persists through
  successful repair bootstraps and daemon restarts, and re-arms when the spec changes. This prevents a successful
  rebuild from erasing the evidence of repeatedly wiped claims.

### Versions

- Kubernetes consumer payloads require provenance capability version 2. Older images fail closed with an upgrade
  reason. Docker payloads keep capability version 1 compatibility. The daemon and performer roll out together.

## Acceptance tests

- Unit: the safe writer refuses a planted temp link and replaces a planted target link without following it.
- Unit: the verify child that reads stdin cannot swallow the script; a later failing check still fails the verify.
- Unit: non-UTF-8 and over-bound payloads are refused with a reason.
- Unit: the verifier pod spec mounts the claim read-only, has no checkout, no credentials, no stdin, and a deadline.
- Unit: project files are fetched at the SHA and placed in the pod, not read from the agent's checkout.
- Unit: the verifier result decides completion on Kubernetes; the bootstrap self-report does not.
- Docker is unchanged.

## Deployment

Roll out daemon and performer together. Schema version is 38, with backward compatible defaults for versions up to
37. The first bootstrap after deploy runs clean verification in one pod, confirms that pod is gone, then runs the
provenance walk in a second pod. Only its small summary is frozen before consumer dispatch is enabled. An older
successful cache without the new summary stays unready and rebootstraps once. A current schema cache restored after restart gets a walk only full recheck before consumer use.
Docker keeps its existing local manifest, clean verification and stat check path.

Set daemon `kubernetes_verifier_image` to an operator trusted performer image containing the current walker.
It defaults to the daemon's `performer_image` (currently `coordinare-performer:full`); a deployment without a usable
image must set it explicitly. In Helm use `config.kubernetes_verifier_image`, with `performers.imagePullSecrets` for
registry access. Symphony performer endpoint images cannot change the verifier image. Use an immutable image digest
when deployment policy requires registry content pinning. Roll daemon and performer together, including the verifier
image. Claim permission requirements concern the other mode bits: files must be world readable and directories
world traversable, with executable cache tools. Apply `o+rX`. The pods run as uid 10001 with no `fsGroup`; do not
rely on owner or group permission bits. They never change claim permissions or ownership.

## Status of this change

**Implemented, including the third security review blockers and follow ups.**

Regression tests were written and observed failing before the implementation changes. The third review fixes are:

1. Verification and provenance use separate sequential pods on the same read only claim. The runtime confirms
   deletion before returning. Only walk output enters the provenance parser. Both pods retain identical hardening.
   Forged verify lines and unterminated verify output cannot affect provenance or the exit code verdict.
2. A claim `PermissionError` is a verdict failure with an actionable `o+rX` reason, naming uid 10001 and no `fsGroup`.
   Deployment guidance specifies the other permission bits.
3. Log reads pass both `tail_lines=32` and `limit_bytes` to Kubernetes, then retain the bounded byte tail locally.
4. Serialized project JSON, including keys and escaping, is capped at 256 KiB. Verify text is capped at 256 KiB.
   Base64 expansion plus setup and pod metadata fit under the 900 KiB serialized pod cap with margin, covered by
   maximum input tests. Walk activation is base64 encoded to prevent JSON escaping from exhausting the pod budget.
5. The verify pod carries no walker sentinel. Its exit code decides verification even with unterminated stdout.

The second security review fixes are covered by regression tests written before implementation:

- Log tail (B1): request the last 32 lines, retain a bounded byte tail, and require exactly one sentinel as the final
  nonempty line in the isolated walk output. Large verify output retains the exit code verdict. Missing final sentinels, including failed walk pods without
  results, are infrastructure during rechecks and never record drift.
- Private registries (B2): the verifier receives the runtime's image pull secrets. ErrImagePull, ImagePullBackOff,
  InvalidImageName and CreateContainerConfigError stop polling immediately with an infrastructure result.
- Pod hardening (B3): fixed UID and GID 10001, runAsNonRoot, RuntimeDefault seccomp, no privilege escalation, all
  capabilities dropped, readOnlyRootFilesystem, and enableServiceLinks disabled. A writable emptyDir at /tmp is also
  HOME. No fsGroup is set, so the verifier does not change claim ownership. Image packages are installed globally and
  readable by this identity. Claim files must have the other read bit, directories the other read and execute bits,
  and tools the other execute bit (`o+rX`). A root bootstrap must preserve these permissions.
- Image and size (B3): both initial verification and rechecks use only the daemon's kubernetes_verifier_image,
  defaulting to its performer_image. Invalid or missing usable images fail configuration clearly. Helm renders the
  same default. The entire serialized pod, including base64 and JSON expansion of all inputs, is bounded to 900 KiB;
  refusal is infrastructure and creates no pod.
- Stat refusal loop (F1): count confirmed consumer stat refusals per symphony across restarts. More than three
  consecutive refusals request regeneration through the existing bootstrap budget, with an operator visible reason.
  Repeated signals during requested regeneration do not create pods or spend again. A successful consumer or new bootstrap freeze resets
  the refusal counter, while successful repairs preserve the repair budget until the spec changes. Infrastructure
  rechecks do not consume the refusal allowance.
- Consumer payload (F2): explicit version 2 requires stat_digest before materialisation. Legacy version 1 remains
  compatible with Docker. Kubernetes dispatch always sends version 2.
- Walker failures (F3): unexpected exceptions, including recursion errors, emit a bounded infrastructure error
  sentinel. Rechecks treat this as transient and retain the frozen content baseline. Claim permission errors are
  explicit verdict failures with the `o+rX` repair instruction.

- B1: scripts are written with a safe create (random temp name, `O_EXCL|O_NOFOLLOW`, `fchmod` on the descriptor, `fsync`,
  rename). A link at the temp name is refused, and a link at the target is replaced, never written through
  (`write_replacing` in `env_provenance.py`).
- B2: the verify text never reaches the verify child on stdin. It is staged in a fresh 0700 directory outside the cache
  and the checkout, run as `bash <file>` with stdin closed, and removed after the run (`_stage_verify_text` in
  `workspace.py`).
- Encodings: text that is not valid UTF-8 is refused with a reason, and the payload bound is in bytes.
- Clean-room verifier pod on Kubernetes: `services/kubernetes_verifier.py` builds the pod (claim mounted read-only at the
  consumer path from the symphony's subPath, no checkout, no secret, no service-account token, no stdin, a deadline,
  limits, and `restartPolicy: Never`). `KubernetesRuntime.run_verifier` runs it, reads the phase, and removes it in every
  path. An API failure is infrastructure (`None`), not a verdict.
- The in-pod setup writes the daemon's verify and the project files (from the commit) into a private directory, and
  resolves `BUNDLE_PATH` the way bundler will see it. A relative path in `.bundle/config` resolves against the cache root,
  where a vendored Gemfile's bundle was installed. `BUNDLE_GEMFILE` is the commit's Gemfile.
- Kubernetes completion: `_verify_bootstrap_on_kubernetes` takes the verifier's verdict, and the bootstrap's self-report
  does not decide. The project files come from `get_file_content` at the bootstrap's SHA.
- B3 and R1: `on_bootstrap_complete` skips the daemon populated check on Kubernetes. `freeze_provenance` consumes the
  validated verifier summary without walking the daemon seed directory or writing a manifest.
- Persona: on Kubernetes the context block comes first and says where verification runs (the verifier pod, no checkout,
  the commit's project files, the read-only cache, the env set). A verify in the container is a pre-check only.
  Persona tests pin that the README reaches the prompt.

**Residuals.**

- R1 closed: the verifier walks the real claim, the daemon freezes bounded inputs and digests, and full checks run
  in walk only pods after restart, on schedule and after consumer stat mismatch. No manifest is streamed.
- R2 closed as part of R1: trusted consumer code checks spec 234's stat digest before frozen activation. A mismatch
  requests a content recheck. A match with stat churn refreshes the baseline without rebuilding.
- R3 closed: consumer refusal spends the 088 budget through `env_cache_health_failed`. Duplicate signals do not
  spend twice while regeneration is requested. Successful repair bootstraps retain that budget until a spec change.
- R4 closed: Kubernetes readiness requires persisted verifier success, frozen content and stat digests, and frozen
  consumer inputs. The dispatch hold no longer checks the daemon seed directory on Kubernetes.
- Older performers require a coordinated rollout. Missing verifier results or capability version 2 are refused.
- Live Kubernetes deployment validation remains operational follow up. Unit coverage uses scripted fake runtimes,
  records pod specs and executes the actual verifier command without a cluster or localhost socket.
