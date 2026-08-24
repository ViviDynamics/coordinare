# Contract: Search-Space Definition

The declarative, validated description of what is swept (FR-001..003).
Models in `src/coordinare/bench/space.py`; the definition is a YAML file.
The shipped default lives at `benchmarks/spaces/default.yaml` over
`benchmarks/spaces/baseline.yaml`. Enforced by `tests/unit/test_136_space.py`.

## Format

```yaml
name: default
baseline_config: baseline.yaml       # resolved relative to this file
dimensions:
  - name: ci-gate
    path: symphonies.bench.persona_scope.ci_gate.enabled
    choices: [false, true]
    description: "075 implementer CI gate"
  - name: implementer-mode
    path: global_config.performers.implementer.mode
    choices: [cheap-tool, premium-tool]
    description: "implementer model tier"
candidates:
  - name: conservative
    description: "all gates on, premium models"
    overrides:
      symphonies.bench.persona_scope.ci_gate.enabled: true
      global_config.performers.implementer.mode: premium-tool
```

## Guarantees

1. **Validated on load** (FR-002): the baseline loads as a valid ROOT
   `CoordinareConfiguration` (`global_config` + `symphonies` — the shape the
   production daemon splits into `state["config"]` / `state["symphony_configs"]`); every `path` resolves in the baseline; every choice
   and every candidate materializes through the real config schema. Errors
   name the offending dimension/candidate and value. Nothing runs on a
   partially valid space.
2. **Explicitly bounded** (FR-003): dimensions listed are swept; everything
   else is fixed at the baseline value; the enumerated point count
   (ablation: 1 + Σ per-dimension non-baseline choices; candidates: K) is
   reported before any run starts. No implicit cross-product exists.
3. **Path semantics** (research R2): dot-separated keys over the config tree;
   the segment after `symphonies` is a symphony **name** (list addressed by
   `SymphonyConfig.name`), so definitions survive list reordering.
4. **Distinct points ⇒ distinct fingerprints** (FR-004): a point's identity is
   the SHA-256 (16 hex) of its materialized config's canonical JSON; identical
   materializations are detected and flagged, and a choice equal to the
   baseline value is skipped with a coverage note (spec edge case).
5. **At least one of** `dimensions` / `candidates` must be non-empty (FR-001);
   an empty definition fails loudly.

## Known-not-sweepable (documented, not invented)

- Blocked-recovery (129): env-var-gated (`COORDINARE_BLOCKED_RECOVERY`), not a
  config field.
- Security-scan (083): no boolean gate field; participates via
  `performers.security.mode`.
