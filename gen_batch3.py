"""Batch-3 generator: split _monitor_performer_body into phase helpers.

Reads monitor_performer.py, extracts the try-body statements into phase
helpers under monitor/body.py, and rewrites monitor_performer.py as
facade + node.

One-shot: run against a checkout where monitor_performer.py still contains
the full _monitor_performer_body (the pre-435 source). It refuses to run
on a tree where the decomposition already happened.
"""
import ast
import subprocess  # noqa: E402
from pathlib import Path

REPO = Path(__file__).parent
SRC = REPO / "src/coordinare/graph/nodes/monitor_performer.py"
OUT = REPO / "src/coordinare/graph/nodes/monitor/body.py"

text = SRC.read_text()
lines = text.splitlines(keepends=False)

import re

if not re.search(r"\basync def _monitor_performer_body\b", text):
    raise SystemExit(
        "monitor_performer.py is already the 435 facade (no _monitor_performer_body); "
        "run this generator from a pre-decomposition checkout instead."
    )

tree = ast.parse(text)
body_fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_monitor_performer_body")
try_stmt = next(n for n in body_fn.body if isinstance(n, ast.Try))

CTX_SEED = [
    "status", "marker", "stage", "card", "card_id", "github", "board_provider",
    "service", "session_id", "timeout_secs", "new_events", "failure_shape",
    "performer_services", "status_payload", "teardown_on_exit",
]
# replaced by MAX_TOTAL cost-based packing

NAMES = {
    349: "_phase_board_reconcile",
    378: "_phase_stall_expired",
    460: "_phase_token_refresh",
    601: "_phase_merge_events",
    687: "_phase_token_counters",
    748: "_phase_security_scan_gate",
    841: "_phase_assessment_complete",
    891: "_phase_qa_passed",
    985: "_phase_status_gate",
    1046: "_phase_stall_watchdog",
    1157: "_phase_terminal_markers",
    1228: "_phase_local_test_gate",
    1350: "_phase_documentation_findings",
    1361: "_phase_terminal_success",
    1585: "_phase_review_routes",
    1759: "_phase_security_failed",
    1842: "_phase_qa_failed",
    1947: "_phase_idle_timeout",
    1880: "_phase_token_limit",
    1957: "_phase_idle_branch",
    2023: "_phase_empty_output",
    2053: "_phase_error_status",
    2167: "_phase_blocked_questions",
    2269: "_phase_feedback_bounce",
    2371: "_phase_no_progress_relay",
}


def dedent_range(start, end):
    """Dedent lines[start-1:end] (1-indexed, inclusive) to column 0."""
    seg = lines[start - 1 : end]
    indents = [len(l) - len(l.lstrip()) for l in seg if l.strip()]
    delta = indents[0]
    return [l[delta:] if l.strip() else "" for l in seg]


def trim_blanks(text_lines):
    out = list(text_lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def make_unit(stmt, orphan_start):
    text_lines = trim_blanks(dedent_range(orphan_start, stmt.end_lineno))
    return {"text": text_lines, "first": orphan_start, "last": stmt.end_lineno, "stmt": stmt}


def collect_names(texts):
    """(loaded, stored) for joined dedented source.

    Comprehension bodies are walked fully: free variables resolve to the
    enclosing scope at runtime and MUST count as loads (a miss here means a
    cross-helper local is never promoted -> NameError). Comp targets don't
    leak, but counting them as stores only over-approximates promotion, which
    is safe for valid code.
    """
    parts = [t if isinstance(t, str) else "\n".join(t) for t in texts if t is not None]
    src = "\n".join(parts)
    mod = ast.parse(src)
    loaded, stored = set(), set()

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name):
                (stored if isinstance(child.ctx, ast.Store) else loaded).add(child.id)
            walk(child)

    for st in mod.body:
        walk(st)
    return loaded, stored


# ---------------------------------------------------------------- partition
def build_units(stmts, prev_end):
    units, cursor = [], prev_end
    for s in stmts:
        units.append(make_unit(s, cursor + 1))
        cursor = s.end_lineno
    return units


try_units = build_units(try_stmt.body, try_stmt.lineno)

all_unit_lines = set()
for u in try_units:
    all_unit_lines.update(range(u["first"], u["last"] + 1))
missing = [
    i for i in range(try_stmt.lineno + 1, 2520) if i not in all_unit_lines and lines[i - 1].strip()
]
if missing:
    raise SystemExit(f"partition gap, lines not covered by any unit: {missing}")

# ---------------------------------------------------------------- specs
ctx_names = set(CTX_SEED)
specs = []


BUDGET = 76  # pessimistic packing budget — emitted helpers stay <= 100 lines
TAIL_FIRST = 2513  # hand-written tail: force a run boundary so the skip check hits


def cost(units_):
    """Pessimistic helper size: preamble/sync bind ALL touched names, not just ctx."""
    texts = [u["text"] for u in units_]
    loaded, stored = collect_names(texts)
    return sum(len(t) for t in texts) + len(loaded | stored) + len(stored) + 4


def out_run(cur, out):
    if not cur:
        return
    pack = []
    for u in cur:
        if pack and cost(pack + [u]) > BUDGET:
            out.append({"kind": "run", "units": pack, "stmt_first": pack[0]["stmt"].lineno})
            pack = []
        pack.append(u)
    if pack:
        out.append({"kind": "run", "units": pack, "stmt_first": pack[0]["stmt"].lineno})


def decompose(units_, out, depth=0):
    cur = []
    for u in units_:
        s = u["stmt"]
        if isinstance(s, ast.Try) and s.lineno == 492 and depth == 0:
            out_run(cur, out)
            cur = []
            out.append({"kind": "poll", "stmt": s, "stmt_first": s.lineno})
            continue
        if s.lineno == TAIL_FIRST and depth == 0:
            out_run(cur, out)
            cur = []
        if len(u["text"]) > BUDGET and isinstance(s, ast.If):
            out_run(cur, out)
            cur = []
            test_src = ast.unparse(s.test)
            guard_ld, _ = collect_names([f"if {test_src}:\n    pass"])
            body_children = []
            decompose(build_units(s.body, s.lineno), body_children, depth + 1)
            # safety: the guard is re-evaluated for the continuation, so the body
            # must not store anything the guard reads
            body_stores = set()
            for child in body_children:
                if child["kind"] == "run":
                    body_stores |= collect_names([u["text"] for u in child["units"]])[1]
            if guard_ld & body_stores:
                raise SystemExit(f"guard {s.lineno} reads names its body stores — unsafe to split")
            out.append({"kind": "wrap", "guard_src": test_src, "stmt_first": s.lineno, "children": body_children})
            if s.orelse:
                orelse_children = []
                decompose(build_units(s.orelse, s.orelse[0].lineno - 1), orelse_children, depth + 1)
                out.append({
                    "kind": "wrap", "guard_src": f"not ({test_src})",
                    "stmt_first": s.lineno, "children": orelse_children,
                })
            continue
        cur.append(u)
    out_run(cur, out)


decompose(try_units, specs)

# cross-pack promotion: loaded in a later spec than a real (function-scope) store.
# Wrapper sub-packs are flattened into the flow: each sub-pack executes in order
# between the wrapper's neighbours, so names stored in one sub-pack and read in
# another (or in a later phase) must ride ctx just like run-to-run locals.
def flatten_per(sp, per):
    if sp["kind"] == "run":
        per.append(collect_names([u["text"] for u in sp["units"]]))
    elif sp["kind"] == "poll":
        ld, _ = collect_names(["status = await service.check_status(x)\n"])
        per.append((ld, set()))
    else:
        test_ld, _ = collect_names([f"if {sp['guard_src']}:\n    pass"])
        per.append((test_ld, set()))  # the wrapper's own condition, before its children
        for child in sp["children"]:
            flatten_per(child, per)


per = []
for sp in specs:
    flatten_per(sp, per)
for i, (ld, _) in enumerate(per):
    for name in sorted(ld):
        if name in ctx_names:
            continue
        for j in range(i):
            if name in per[j][1]:
                ctx_names.add(name)
                print(f"promoted to ctx: {name}")
                break


def preamble_and_syncs(texts):
    loaded, stored = collect_names(texts)
    return sorted((loaded | stored) & ctx_names), sorted(stored & ctx_names)


def helper_head(name, doc, extra_args=()):
    args = ["    state: CoordinareState,", "    ctx: _BodyCtx,"] + list(extra_args)
    out = [f"async def {name}(", *args, ") -> CoordinareState | None:"]
    out.append(f'    """{doc}"""')
    return out


def indented(text_lines):
    return [("    " + l) if l.strip() else "" for l in text_lines]


def emit(name, texts, doc="Phase helper: return a state to short-circuit, or None to continue."):
    pre, sync = preamble_and_syncs(texts)
    out = helper_head(name, doc)
    for n in pre:
        out.append(f"    {n} = ctx.{n}")
    for t in texts:
        out.extend(indented(t))
    for n in sync:
        out.append(f"    ctx.{n} = {n}")
    out.append("    return None")
    return "\n".join(out) + "\n"


HELPERS = []
PHASE_LIST = []


def emit_spec(name, sp):
    """Emit one spec (run or nested wrap) into HELPERS under `name`."""
    if sp["kind"] == "run":
        HELPERS.append(emit(name, [u["text"] for u in sp["units"]]))
    else:  # nested wrap
        emit_wrap(sp, name, named=False)


def emit_wrap(sp, wrapper, named=True):
    """Emit a wrapper helper (guard + sequential child calls) and its children."""
    cond = sp["guard_src"]
    subnames = []
    for k, child in enumerate(sp["children"], 1):
        cname = f"{wrapper}_s{k}"
        subnames.append(cname)
        emit_spec(cname, child)
    test_ld, _ = collect_names([f"if {cond}:\n    pass"])
    out = helper_head(wrapper, "Phase helper: return a state to short-circuit, or None to continue.")
    for n in sorted(test_ld & ctx_names):
        out.append(f"    {n} = ctx.{n}")
    out.append(f"    if {cond}:")
    out.append("        for _sub in (" + ", ".join(subnames) + ",):")
    out.append("            _result = await _sub(state, ctx)")
    out.append("            if _result is not None:")
    out.append("                return _result")
    out.append("    return None")
    HELPERS.append("\n".join(out) + "\n")
    if named:  # only top-level wraps join the phase chain; nested wraps are
        PHASE_LIST.append(wrapper)  # reached through their parent's child loop
        print(f"wrap {sp['stmt_first']:5d}  {wrapper}")


for sp in specs:
    if sp["kind"] == "run":
        if sp["stmt_first"] == 2513:  # tail — hand-written _phase_in_progress
            continue
        name = NAMES.get(sp["stmt_first"], f"phase_{sp['stmt_first']}")
        HELPERS.append(emit(name, [u["text"] for u in sp["units"]]))
        PHASE_LIST.append(name)
        print(f"run  {sp['units'][0]['last']:5d}  {name}")
    elif sp["kind"] == "poll":
        HELPERS.append(
            "\n".join(
                [
                    "async def poll_service(",
                    "    state: CoordinareState,",
                    "    ctx: _BodyCtx,",
                    ") -> CoordinareState | None:",
                    '    """Poll the active performer service (verbatim except for handler extraction)."""',
                    "    status_payload = ctx.status_payload",
                    "    service = ctx.service",
                    "    session_id = ctx.session_id",
                    "    try:",
                    "        status = await service.check_status(str(session_id), payload=status_payload)",
                    "    except (TransportError, ConnectionError, TimeoutError) as exc:",
                    "        return await poll_transport_error(state, ctx, exc)",
                    "    except PermanentGitHubError as exc:",
                    "        return await poll_permanent_error(state, ctx, exc)",
                    "    ctx.status = status",
                    "    return None",
                ]
            )
            + "\n"
        )
        PHASE_LIST.append("poll_service")
        s = sp["stmt"]
        for h, hname, doc in (
            (s.handlers[0], "poll_transport_error", "Transport-error handler (verbatim except clause)."),
            (s.handlers[1], "poll_permanent_error", "Permanent-error handler (verbatim except clause)."),
        ):
            orphan = h.body[0].lineno - 1  # line before first handler-body stmt
            h_units = build_units(h.body, orphan)
            pre, _sync = preamble_and_syncs([u["text"] for u in h_units])
            out = [
                f"async def {hname}(",
                "    state: CoordinareState,",
                "    ctx: _BodyCtx,",
                "    exc: Exception,",
                ") -> CoordinareState:",
                f'    """{doc}"""',
            ]
            for n in pre:
                out.append(f"    {n} = ctx.{n}")
            for u in h_units:
                out.extend(indented(u["text"]))
            HELPERS.append("\n".join(out) + "\n")
    else:  # top-level wrap
        wrapper = NAMES.get(sp["stmt_first"], f"wrapper_{sp['stmt_first']}")
        emit_wrap(sp, wrapper)

# ---------------------------------------------------------------- body.py
header = "\n".join(lines[9:162])  # from __future__ ... through progress_evidence import
dataclass_fields = "\n".join(
    f"    {n}: Any = None" for n in sorted(ctx_names) if n not in ("state",)
)
body_doc = '''"""Phase helpers for the monitor_performer body (435 batch 3).

Split from ``monitor_performer._monitor_performer_body`` verbatim: each
``_phase_*`` helper receives ``(state, ctx)`` and returns a state to
short-circuit-return or ``None`` to fall through to the next phase.
"""

'''
orchestrator = '''

async def _monitor_performer_body(state: CoordinareState) -> CoordinareState:
    """Poll the active performer and route based on status.

    Reads ``performer_stage`` from state, resolves the service from
    ``performer_services[performer_stage}```, and polls ``check_status``.
    Contains zero role-specific logic — all routing is driven by the
    status string returned by the performer.
    """
    # 030: Reset requirement-change flags at cycle start so stale flags never persist.
    # Placed before all early-return paths (incl. override check, service/card guard).
    state["requirements_changed"] = False
    state["requirements_changed_details"] = {}

    card = state.get("current_card")
    github = state.get("github_service")
    board_provider = board_of(state)

    override_state = await _phase_override(state, card, github, board_provider)
    if override_state is not None:
        return override_state

    ctx = _build_monitor_ctx(state)
    if ctx.bail:
        return state

    gate_result = await _phase_ephemeral_gate(state, ctx)
    if gate_result is not None:
        return gate_result

    slot_result = await _phase_slot_setup(state, ctx)
    if slot_result is not None:
        return slot_result

    try:
        for _phase in _PHASES:
            _result = await _phase(state, ctx)
            if _result is not None:
                return _result
        return await _phase_in_progress(state, ctx)
    finally:
        if ctx.teardown_on_exit:
            await _teardown_workspace(state)
'''

# ---------------------------------------------------------------- setup phases
def verbatim_helper(name, doc, start, end, extra_args=()):
    out = helper_head(name, doc, extra_args=extra_args)
    out.extend(indented(trim_blanks(dedent_range(start, end))))
    out.append("    return None")
    return "\n".join(out) + "\n"


override_helper_lines = [
    "async def _phase_override(",
    "    state: CoordinareState,",
    "    card: Any,",
    "    github: Any,",
    "    board_provider: Any,",
    ") -> CoordinareState | None:",
    '    """031: human-override handling; returns state when handled, None to continue."""',
]
override_helper_lines.extend(indented(trim_blanks(dedent_range(202, 246))))
override_helper_lines.append("    return None")
override_helper = "\n".join(override_helper_lines) + "\n"
ephemeral_gate_helper = emit(
    "_phase_ephemeral_gate",
    [trim_blanks(dedent_range(259, 314))],
    doc="077: ephemeral-implementer CI-gate re-evaluation (no live session to poll).",
)

# slot setup: drop the verbatim teardown latch (lines 344-346); _BodyCtx defaults it.
slot_text = [l for l in trim_blanks(dedent_range(316, 346)) if not l.startswith("_teardown_on_exit")]
slot_text.append("# teardown latch: ctx.teardown_on_exit defaults True (see _BodyCtx).")
slot_setup_helper = emit(
    "_phase_slot_setup",
    [slot_text],
    doc="048/027: slot acquisition, session id, role timeout, teardown latch.",
)

build_ctx_helper = '''def _build_monitor_ctx(state: CoordinareState) -> _BodyCtx:
    """Bind the phase-threading locals into a _BodyCtx (verbatim from the body prologue)."""
    stage: str = state.get("performer_stage", "implementing")
    performer_services: dict[str, Any] = state.get("performer_services") or {}

    # card_id must be extracted before SlotManager lookup.
    # Also guards against missing/invalid current_card — if card is not a
    # dict we can't acquire a meaningful slot and should bail early.
    bail = not isinstance(state.get("current_card"), dict)
    if bail:
        state["phase"] = "idle"
    card = state.get("current_card")
    card_id = str(card.get("id", "")) if not bail else ""
    return _BodyCtx(
        stage=stage,
        performer_services=performer_services,
        bail=bail,
        card=card,
        card_id=card_id,
        github=state.get("github_service"),
        board_provider=board_of(state),
        teardown_on_exit=True,
    )
'''

tail_phase = '''async def _phase_in_progress(state: CoordinareState, ctx: _BodyCtx) -> CoordinareState:
    """In-progress (working): workspace stays active; refresh backend UI."""
    ctx.teardown_on_exit = False  # still working — workspace stays active
    state["phase"] = "monitoring_performer"

    # 052: Backend transparency — discover UI URL and poll session stats.
    await _refresh_backend_ui(state, ctx.service, ctx.card_id)

    return state
'''

# ---------------------------------------------------------------- body.py
header = "\n".join(lines[9:162])  # from __future__ ... through progress_evidence import
header = header.replace(
    "from typing import TYPE_CHECKING, Any",
    "from dataclasses import dataclass\nfrom typing import TYPE_CHECKING, Any",
)
dataclass_fields = ["    bail: bool = False"]
for n in sorted(ctx_names):
    if n == "state":
        continue
    if n == "teardown_on_exit":
        dataclass_fields.append("    teardown_on_exit: bool = True")
    else:
        dataclass_fields.append(f"    {n}: Any = None")

body_doc = '''"""Phase helpers for the monitor_performer body (435 batch 3).

Split from ``monitor_performer._monitor_performer_body`` verbatim: each
``_phase_*`` helper receives ``(state, ctx)`` and returns a state to
short-circuit-return or ``None`` to fall through to the next phase.
"""

'''
orchestrator = '''

async def _monitor_performer_body(state: CoordinareState) -> CoordinareState:
    """Poll the active performer and route based on status.

    Reads ``performer_stage`` from state, resolves the service from
    ``performer_services[performer_stage]``, and polls ``check_status``.
    Contains zero role-specific logic — all routing is driven by the
    status string returned by the performer.
    """
    # 030: Reset requirement-change flags at cycle start so stale flags never persist.
    # Placed before all early-return paths (incl. override check, service/card guard).
    state["requirements_changed"] = False
    state["requirements_changed_details"] = {}

    card = state.get("current_card")
    github = state.get("github_service")
    board_provider = board_of(state)

    override_state = await _phase_override(state, card, github, board_provider)
    if override_state is not None:
        return override_state

    ctx = _build_monitor_ctx(state)
    if ctx.bail:
        return state

    gate_result = await _phase_ephemeral_gate(state, ctx)
    if gate_result is not None:
        return gate_result

    slot_result = await _phase_slot_setup(state, ctx)
    if slot_result is not None:
        return slot_result

    try:
        for _phase in _PHASES:
            _result = await _phase(state, ctx)
            if _result is not None:
                return _result
        return await _phase_in_progress(state, ctx)
    finally:
        if ctx.teardown_on_exit:
            await _teardown_workspace(state)
'''

phase_list_src = "_PHASES = (\n" + "".join(f"    {p},\n" for p in PHASE_LIST) + ")\n"

BODY = "\n".join(
    [
        body_doc,
        header,
        "\n\n@dataclass\n",
        "class _BodyCtx:\n    \"\"\"Cross-phase locals threaded through the monitoring body.\"\"\"\n",
        "\n".join(dataclass_fields),
        "\n\n",
        override_helper,
        build_ctx_helper,
        ephemeral_gate_helper,
        slot_setup_helper,
        "\n".join(HELPERS),
        tail_phase,
        phase_list_src,
        orchestrator,
    ]
)
OUT.write_text(BODY)
print(f"wrote {OUT} ({len(BODY.splitlines())} lines)")

# ------------------------------------------- rewrite monitor_performer.py
node_lines = lines[164:183]  # monitor_performer node (165-183)
mp = "\n".join(lines[0:45])
mp += "\nfrom coordinare.graph.nodes.monitor.body import _monitor_performer_body\n"
mp += "\n".join(lines[45:183])
# body moved to monitor.body: prune now-dangling code lines the ruff pass flags.
mp += "\n"
SRC.write_text(mp)
print(f"rewrote {SRC} ({len(mp.splitlines())} lines)")
print(f"\n{len(HELPERS)} phase helpers, {len(PHASE_LIST)} phases")
