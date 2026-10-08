# Cluster streaming validation

Issue #527

## Scope

Run a real implementer card through a temporary cluster daemon, Kubernetes
performer launch, `always` proxy, Claude Code CLI, and board hand-off. Record
timing, incremental stream delivery, tool execution, and buffered rendering
parity in the spec-080 findings. File observed regressions separately.

## Assumptions

- The existing daemon and board remain owned by their current workload.
- A dedicated board and temporary daemon use the existing sample repository.
- Initial model legs use GLM Flash; later retries pair Qwen
  planning with GLM execution and supported generation forwarding.
- The current release supplies both daemon and performer images.
- Temporary measurement observes event timing and counts without publishing
  credentials or raw prompts. Unexercised backends are reported explicitly.
- Completing the implementer stage and entering review satisfies T037; merging
  the sample PR is outside this validation's acceptance criteria.
- An additional dedicated Codex card exercises the Responses wire. After the
  namespace-wide cleanup regression was observed, validation daemons run
  sequentially. Both setup failures and interrupted runs remain in the report.

## Tasks

- [x] 1. Validate isolated deployment configuration and seed the dedicated board.
- [x] 2. Run the real card and capture stream, CLI, tool and board evidence.
- [x] 3. Record per-backend verdicts and regressions, clean temporary resources.
- [ ] 4. Review and merge the findings PR through the repository gates.
