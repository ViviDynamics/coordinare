# Contract: RoleWorkflow ↔ performer

**Direction**: performer core → workflow. Internal to the performer; the
coordinare/performer wire contract is unchanged by this feature.

```python
class RoleWorkflow(Protocol):
    name: str

    async def run(
        self,
        stand: Stand,          # workspace (head worktree)
        score: Score,          # the task: card, criteria, diff, role config
        toolkit: Toolkit,      # execution primitives
    ) -> WorkflowResult: ...
```

## Toolkit

The workflow may only reach the outside world through these. A workflow that
imports `subprocess` or `httpx` directly is a contract violation.

```python
class Toolkit(Protocol):
    async def run_command(self, cmd: list[str], *, cwd: Path | None = None,
                          timeout_s: int = 300) -> ExecutedCheck: ...
    async def capture_screenshot(self, url: str, *, path: Path) -> Path | None: ...
    async def dom_snapshot(self, url: str) -> list[Observation]: ...
    async def call_model(self, *, persona: str, schema: type[BaseModel],
                         content: list[dict], budget: Budget) -> BaseModel: ...
    def get_backend(self, name: str) -> BackendAdapter: ...
    def emit(self, event: BackendEvent) -> None: ...
```

### Guarantees the toolkit makes

1. `call_model` enforces the per-run model-call ceiling. Exceeding it raises
   `ModelCallCeilingExceeded`; it never silently continues.
2. `call_model` retries once at double budget on `finish_reason: length`, and
   surfaces truncation as `TruncatedResponse`, never as a parse error.
3. `call_model` validates against `schema`, reprompts exactly once on violation,
   then raises `SchemaViolation`.
4. `run_command` returns a real `exit_code`. It never infers success.
5. `dom_snapshot` labels come from the DOM. The model is never asked for them.
6. `capture_screenshot` returns `None` rather than a path it did not verify on
   disk (inherited from `qa_capture`).

### Guarantees the workflow makes

1. Every criterion reported as passed is bound to at least one `ExecutedCheck`.
2. A step that cannot run emits a synthetic finding and fails closed (R7); it
   never omits the check silently.
3. No `Finding` prescribes a fix.
4. Steps are pure with respect to the toolkit: given the same inputs and the same
   toolkit responses, a step produces the same output. This is what makes
   recorded-fixture tests deterministic.
