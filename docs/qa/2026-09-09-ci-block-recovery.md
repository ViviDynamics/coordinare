# CI failure triage and blocked notifications (#319, #320)

CI check conclusions alone are insufficient to authorize a code-repair turn.
Failed runner setup is an environment hold. If GitHub refuses access to failure
annotations or job evidence, the performer now reports an explicit evidence-access
hold instead of silently exhausting model repair attempts. Grant the performer
credential Checks and Actions read access; repository write access alone does not
provide these permissions. Recognized setup evidence takes precedence over the
access fallback. The recorded /opt/hostedtoolcache permission failure also has a
narrow runner signature; arbitrary application permission failures remain code
failures. Do not weaken code-quality gates or broadly chmod runner directories.

For an already-blocked website card, repair the runner setup and/or evidence
permissions, verify the same PR head's checks, then explicitly requeue the card
through the supported board workflow. Preserve its existing branch and PR.
No existing blocked cards are automatically requeued by this patch.

A blocked notification uses the card's actual open question/reason. The global
blocked_by_dependencies field describes the first filtered TODO card on the board;
it must not override a different performer's CI failure. Real dependency failures
remain visible through their own question and board dependency diagnostics.

Deployment verification: the currently used local full image contains the same
main.py, github.py, ci_evidence.py, infrastructure.py and implementer/ci.py bytes as
main 5014d48. This incident is not explained by those modules being absent from
the image. Build and deploy the patched performer image before resuming QA.
