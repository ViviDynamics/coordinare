# 175 — Complete structured analysis → documenter integration

Closes #256. The prerequisite QA workflow (#255/#257) and role workflows
165/166/169/170/171 are already merged. This completes their integration rather
than replacing their schemas or making the legacy free-form paths mandatory.

## Acceptance

1. Assessor, architect, reviewer, security and QA structured results feed a bounded,
   persisted per-role documentation input. Each input identifies its source head and
   content hash. Review and security never overwrite each other's evidence. A new
   analysis clears that role's stale input until it completes; raw logs/diffs are excluded.
2. The configured documenter receives these inputs in both early and final runs.
   Early runs use a valid Score, the symphony-effective workflow/model/tuning and
   repository settings. Updated findings schedule another run after the previous
   run ends; empty documentation briefs retain the small-card fast path.
3. The documenter remains the sole living-document writer. Existing structured,
   write-free analysing workflows remain unchanged. It integrates findings with
   inventory/citations rather than asking analysing roles to author new prose files.
4. A side run updates the canonical card session even after fanout replaces its
   dictionary. Running jobs resume polling after restart, with bounded failure.
   Final documentation waits for a running early writer; early runs do not start
   beside final documenting/closing stages. Existing non-force push/rebase stays.
5. Early documenter writes remain under docs/; root agent pointers belong to final
   reconciliation. The distinction is explicit in the payload. Implementation
   receives the latest completed documentation paths/head alongside its existing
   implementation brief, and continues using that structured brief as authoritative.
6. Backward snapshots and unconfigured workflows continue loading. No live config
   is enabled automatically. Final SHA-gated documentation still reconciles actual
   code and the legacy architecture_plan_path hand-off remains for legacy roles.

## Answers to the original design questions

Structured role records plus the existing blueprint briefs are the hand-off.
Living prose stays in docs/wiki, with its generated index and final agent pointers.
The early documenter is a side service; it does not become a blocking planning
stage. Serialized documentation writers plus fetch/rebase protect shared branches.
The final documenting stage remains as a reconciliation pass, not the first source
of implementation guidance. Workflow architect plans live in state; legacy plans
remain supported when that workflow is absent.
