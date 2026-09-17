"""Security workflow scanner: runs model-determined security tooling, normalizes output.

Spec 366: The model determines what security tooling applies to this repository.
The scan runs inside the performer, fails closed on any tool problem (missing,
crash, unparseable output, tool not applicable), and produces normalized findings
matching the coordinare's security_scanner.py output for parity (FR-020).

This reverses spec 170's design (scan before model call) — the justification is
that a mechanical floor that scans nothing on three of four languages is not a
floor. Abstention now properly reads as unavailable (env_blocked) not as a pass.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any, Callable

import structlog

from performer.workflows.security.budgets import SecurityBudgets
from performer.workflows.security.gate import normalise_tool_path
from performer.workflows.security.models import ScanResult

logger = structlog.get_logger(__name__)

# CWE numbers whose semgrep WARNING findings escalate to high rather than medium.


class ScannerUnavailable(Exception):
    """Raised when a scanner tool is missing, crashes, exits abnormally, or emits unparseable output.

    ``results`` holds the ScanResults of the tools that completed before the failure.
    """

    results: list = []

    def __init__(self, tool: str, reason: str):
        self.tool = tool
        self.reason = reason
        super().__init__(f"{tool}: {reason}")






class NothingToScan(Exception):
    """412: the parsed diff carried no files at all.

    Raised instead of the old ``([], [])`` return, which the gate read as a
    clean scan -- a binary-only or lockfile-only diff (the coordinare sanitizer
    drops those sections before injection) passed with a ``security_passed``
    verdict while scanning nothing.
    """


#: 412: extensions a static security scanner can actually read. Deliberately
#: conservative and code-owned: this is what decides ``not_applicable`` from
#: file types, NOT the model's tool guess. Lockfiles and binary formats are
#: absent by construction.
_SCANNABLE_EXTENSIONS = frozenset({
    "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "go", "rs", "java", "cs", "rb",
    "php", "c", "cc", "cpp", "cxx", "h", "hh", "hpp", "sh", "bash", "zsh", "ksh",
    "ps1", "psm1", "pl", "pm", "lua", "kt", "kts", "swift", "scala", "tf",
    "tfvars", "vue", "svelte", "ex", "exs", "erl", "hrl", "clj", "cljs", "hs",
    "m", "mm", "sql", "dart", "groovy", "yml", "yaml", "json", "toml",
})
_SCANNABLE_NAMES = ("dockerfile", "makefile", "jenkinsfile", "vagrantfile")
#: 412 round 30: ``Dockerfile.md`` / ``Makefile.txt`` are documentation, not
#: scan targets -- a name-based classification must not be defeated by a
#: docs extension bolted onto a tool config name.
_DOCS_SUFFIXES = frozenset({"md", "rst", "txt", "markdown", "adoc", "texi", "info"})
#: 412 round 5: dependency MANIFESTS, not lockfiles. A change to one of these
#: is exactly how a vulnerable dependency enters a PR, so the
#: vulnerable_dependency/supply-chain categories must stay reachable for
#: them; the generated files (_LOCKFILE_NAMES) stay excluded.
_SCANNABLE_MANIFEST_NAMES = frozenset({
    "requirements.txt", "pyproject.toml", "setup.py", "pipfile",
    "gemfile", "go.mod", "package.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "composer.json", "cargo.toml",
})
_LOCKFILE_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "cargo.lock", "poetry.lock", "gemfile.lock",
    "go.sum", "composer.lock", "pipfile.lock", "pnpm-lock.yaml", "pdm.lock",
    "bun.lockb", "pubspec.lock", "packages.lock",
})


def _is_requirements_manifest(name: str) -> bool:
    """412 round 28: the requirements family is dependency manifests only --
    a bare prefix match swept in documentation like ``requirements.md``,
    which then entered the model/tool path instead of the code-decided
    ``not_applicable`` verdict a docs-only change earns."""
    return name == "requirements.txt" or (
        name.startswith("requirements") and name.endswith((".txt", ".in"))
    )


def is_scannable_source(path: str) -> bool:
    """True when a static scanner could read this file's content (412)."""
    name = str(path).strip().rsplit("/", 1)[-1].lower()
    if not name:
        return False
    if name in _LOCKFILE_NAMES or name.endswith(".lock"):
        return False
    if name == ".env" or name.startswith(".env."):
        # 412 round 6: env templates are a committed-secrets scan target.
        return True
    if name in _SCANNABLE_MANIFEST_NAMES or _is_requirements_manifest(name):
        return True
    if any(name.startswith(n) for n in _SCANNABLE_NAMES):
        # 412 round 30: exact names are targets; ``name.ext`` variants are
        # targets unless the extension is documentation (``Dockerfile.md``,
        # ``Makefile.txt``) -- a bare ``startswith`` swept those in.
        return name.rsplit(".", 1)[-1] not in _DOCS_SUFFIXES if "." in name else True
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    return suffix in _SCANNABLE_EXTENSIONS


def has_scannable_source(paths: list[str]) -> bool:
    """412: the not_applicable decision is made from file types, in code."""
    return any(is_scannable_source(p) for p in paths)


#: 412: the argv token the model uses where the changed-file list belongs;
#: code substitutes the real list, so scoping to changed files is mechanical.
FILES_TOKEN = "{{files}}"

#: 412 round 11: options that consume the next argv element as a value. A
#: broad path landing there is not a scan operand (`--exclude pkg/...`).
#: 412 round 16: short options match exactly (--check must not be read as a
#: `-c` value); the spelled-out flags keep prefix matching so the option
#: families (``--exclude-dir``, ``-exclude-dir``) stay covered.
#: 412 round 28: anything OUTSIDE this set makes the following path
#: ambiguous -- ambiguous paths are refused (412 round 44 superseded the
#: preserve-and-append compromise: preserving let an operand-shaped token
#: read repository code beside the changed set, and replacing risked the
#: false-pass direction), so the whitelist only has to be right, not
#: complete.
_VALUE_OPTION_EXACT = frozenset({"-c", "-o", "-f"})
_VALUE_OPTION_PREFIXES = ("--config", "--exclude", "-exclude", "--output", "--format")

def _scoped_argv(argv: list[str], files: list[str], repo_root: Path | None = None) -> list[str]:
    """Substitute the changed-file list into the argv, by code.

    ``{{files}}`` expands to one argv element per changed file. A broad path
    operand (``.``, ``./...``, ``/``, a whole-subtree ``pkg/...``, any
    directory inside the workspace) is replaced by the changed files, token
    or not: ``semgrep . {{files}}`` must not scan the whole workspace beside
    the changed set. Once the files are in place, later broad operands and
    duplicate tokens are dropped rather than re-expanding; without either,
    the files are appended.
    """
    out: list[str] = []
    injected = False
    # 412 round 40: the subcommand slot is any bare token before the first
    # operand -- ``trivy --quiet fs .`` is as valid as ``trivy fs .`` -- so
    # recognition follows the option prefix rather than the fixed position 1.
    operand_seen = False
    for i, element in enumerate(argv):
        # 412 round 9: an option VALUE that happens to be a workspace
        # directory (`semgrep --config rules/`) is not a scan operand --
        # replacing it breaks the scan; the files are appended instead.
        # 412 round 11: the same guard covers every broad operand, keyed on
        # the options that take a value: `--exclude pkg/...` replaced by the
        # changed-file list would exclude the changed files themselves and
        # read as a clean pass on zero evidence. An option spelled with its
        # value inline (`--exclude=vendor/...`) is its own token and never an
        # operand. ``{{files}}`` substitution stays keyed on the known
        # value-takers only: the placeholder is an explicit request for the
        # changed list, wherever the model put it.
        # 412 round 28/29: an option OUTSIDE the whitelist makes the
        # following path ambiguous -- value or boolean-flag operand? Either
        # way the command can be corrupted by replacing it
        # (``--json-output results.json`` overwriting an output file), so a
        # narrow path cannot be rewritten in place: 412 round 44 refuses the
        # plan (the safe hold; the model re-plans with inline spelling or
        # ``{{files}}``) instead of preserving it. A BROAD operand has no
        # scoped reading -- both interpretations scan the whole workspace --
        # so it stays a target and is replaced: the worst case is a tool
        # rejecting the mangled option, which holds the card (the safe
        # side).
        option_value = i > 0 and _option_takes_value(argv[i - 1])
        # 412 round 26: a known subcommand of the tool is CLI plumbing, not
        # a filesystem operand -- ``trivy fs`` must not be repointed at the
        # changed files just because the workspace contains a directory
        # named ``fs`` (the repoint corrupts the CLI shape into a hold).
        # 412 round 40: recognition mirrors the tool's own parser -- the
        # bare token before the first operand is the subcommand slot, so
        # global options before it do not hide the subcommand. Option
        # VALUES on the whitelist are plumbing too and do not end the
        # prefix (``--format json fs``); any other bare token does
        # (positional argv has begun). Recognizing one operand-shaped token
        # too many only leaves it in argv unreplaced -- the safe direction;
        # repointing a real subcommand is the corruption.
        subcommand = (
            i > 0
            and not operand_seen
            and element in _TOOL_SUBCOMMANDS.get(Path(str(argv[0])).stem.lower(), set())
        )
        if i > 0 and not element.startswith("-") and not option_value and not subcommand:
            operand_seen = True
        is_broad = not element.startswith("-") and not subcommand and (
            element in _BROAD_SCAN_OPERANDS
            or element.endswith("/...")
            or _is_workspace_dir(element, repo_root)
        )
        # 412 round 27: a token naming a changed-set element is a target even
        # with a leading dash (``-flag.py`` is a real filename) -- without
        # this the option-token stays in argv and the appended change set
        # rides behind a broken command. The injection spells it ``./``-safe.
        names_changed = _names_changed_set_element(element, files)
        # 412 round 32: an inline option value naming a broad workspace path
        # (``gitleaks --source=.``) scans the whole repository behind the
        # appended change set, so the plan cannot be scoped as written. It
        # cannot be rewritten either -- splitting ``--exclude=vendor/...``
        # would replace the value with the change set and exclude exactly
        # the files the scan must cover -- so the plan is refused and the
        # card holds; the model re-plans with a scoped form (a split broad
        # operand is rewritten, or ``{{files}}`` is used). A whitelisted
        # value-taker keeps the rounds-9-13 contract (its inline value is
        # plumbing: ``--config=rules/``, ``--exclude=vendor/...``), and an
        # inline token naming a changed-set element is already scoped.
        inline_name, inline_sep, inline_value = element.partition("=")
        if (
            inline_sep
            and element.startswith("-")
            and not names_changed
            and inline_name not in _VALUE_OPTION_EXACT
            and not any(inline_name.startswith(p) for p in _VALUE_OPTION_PREFIXES)
            and (
                inline_value in _BROAD_SCAN_OPERANDS
                or inline_value.endswith("/...")
                or _is_workspace_dir(inline_value, repo_root)
            )
        ):
            raise ScannerUnavailable(Path(str(argv[0])).stem, f"unscoped inline operand {element}")
        ambiguous_value = (
            i > 0
            # 412 round 30: ``--`` is the unambiguous end-of-options
            # delimiter, not an unknown value-taking option -- the operand
            # after it is a plain positional and stays a target.
            and argv[i - 1] not in ("-", "--")
            and argv[i - 1].startswith("-")
            and "=" not in argv[i - 1]
            and not option_value
            and not is_broad
        )
        is_target = (
            i > 0
            and not subcommand
            and not option_value
            and (
                names_changed
                or (
                    not ambiguous_value
                    and not element.startswith("-")
                    and (is_broad or _is_workspace_file(element, repo_root) or _is_relative_glob(element))
                )
            )
        )
        # 412 round 13: a bare operand naming an existing workspace path is
        # a scan target wherever it sits -- a literal file the model kept
        # from its plan (`bandit old.py`) must not be scanned beside the
        # changed set, or pre-existing findings outside the diff block this
        # PR. Nonexistent relative tokens (trivy's ``fs`` subcommand) and
        # absolute paths (report paths, caches) stay alone; replacing tool
        # plumbing fails the tool, which holds the card anyway.
        # 412 round 15: an operand naming a changed path is a target even
        # when the path does not exist on disk (a deletion, a named phantom
        # beyond the truncation cap) -- the workflow deliberately excluded
        # it, so it cannot re-enter the argv beside the changed set.
        # ``{{files}}`` substitutes wherever it appears as an operand, but
        # 412 round 17: in an option-value position (`--exclude {{files}}`)
        # substituting would pass the changed files as exclusion values --
        # the tool scans its default tree and the run reads as clean. The
        # literal token stays, the tool fails on the nonexistent path, and
        # the card holds: the safe side.
        # 412 round 41: a bare operand naming a path outside the workspace
        # is a second scan target the scoping cannot bound -- the tool reads
        # it beside the changed set and its findings can carry the file's
        # contents into the report. 412 round 43: this holds after an
        # unknown option too -- value or read-target is indistinguishable
        # without per-tool tables, and the report-path shapes re-plan with
        # inline spelling (``--path=/tmp/out.json``), so the spaced
        # absolute form is refused rather than preserved.
        if (
            i > 0
            and not element.startswith("-")
            and not option_value
            and not subcommand
            and _names_outside_workspace(element, repo_root)
        ):
            raise ScannerUnavailable(Path(str(argv[0])).stem, f"unscoped operand {element} outside the workspace")
        # 412 round 44: a narrow token after an unknown option is the last
        # unscoped-reading channel -- preserved as the round-28 compromise
        # wrote it, the tool reads repository code beside the changed set
        # when the token was an operand (``--source old.py``). Replacing it
        # risks the round-28 false-pass direction (a replaced value, a scan
        # on nothing reading as clean), so the plan is refused and the
        # model re-plans: inline spelling for value positions, ``{{files}}``
        # or a scoped operand for targets. Broad operands after unknown
        # options keep the round-29 replacement (both readings scan the
        # whole workspace; the worst case is a tool rejecting the mangled
        # option, which holds the card). 412 round 15: a token naming a
        # changed path is a target wherever it sits -- the workflow
        # deliberately excluded that path, so re-reading it is the change
        # set's own scope, not an unscoped read -- which covers the common
        # ``semgrep --json <literal changed paths>`` plans; the ``{{files}}``
        # placeholder and subcommand tokens are plumbing wherever they sit.
        if ambiguous_value and not names_changed and not subcommand and element != FILES_TOKEN:
            raise ScannerUnavailable(
                Path(str(argv[0])).stem, f"ambiguous operand {element} after {argv[i - 1]}",
            )
        if (element == FILES_TOKEN and not option_value) or is_target:
            if not injected:
                # 412 round 24: a changed path that begins with ``-`` would
                # parse as an option -- spell it as a relative path so the
                # tool reads it as a target and cannot alter the command.
                out.extend(_argv_safe(f) for f in files)
                injected = True
            continue
        # 412 round 33: round 17 kept the literal token in a value position
        # on the assumption the tool fails on the nonexistent path -- but
        # nothing validates that, and a tool that ignores an unknown value
        # scans its default tree unscoped (or writes a literal
        # ``{{files}}`` path). That is a malformed plan, not a scoping
        # problem: refuse it and the card holds.
        if element == FILES_TOKEN and option_value:
            raise ScannerUnavailable(
                Path(str(argv[0])).stem, "malformed plan: {{files}} in an option-value position",
            )
        out.append(element)
    if not injected:
        out.extend(_argv_safe(f) for f in files)
    return out


def _argv_safe(path: str) -> str:
    return f"./{path}" if path.startswith("-") else path


def _option_takes_value(token: str) -> bool:
    """True when ``token`` is a KNOWN option that consumes the NEXT argv
    element as its value.

    412 round 11: options whose next argv element is a value, not an
    operand. Flags like ``--json`` or ``--baseline`` take none, so a broad
    operand after them is still a scan target and must be replaced. 412
    round 16: short options match exactly (--check takes no value, so a
    broad operand after it is still a target); long options keep prefix
    matching so the option families (``--exclude-dir``) stay covered. 412
    round 13: a value attached inline (``--config=rules``) is its own token
    -- the NEXT element is not consumed, so a broad operand after it is
    still a scan target. 412 round 28: the whitelist no longer needs to be
    exhaustive -- an option outside it makes the following path ambiguous
    (see _scoped_argv), and ambiguity preserves the plan as written instead
    of replacing it."""
    if "=" in token:
        return False
    if token in _VALUE_OPTION_EXACT:
        return True
    return any(token.startswith(p) for p in _VALUE_OPTION_PREFIXES)


def _is_relative_glob(element: str) -> bool:
    """True when the operand is a relative glob (``src/**/*.py``).

    412 round 14: globs name no existing path and no directory, but the tools
    expand them to pre-existing files outside the diff -- replaced by the
    changed set like any broad operand. Absolute globs stay alone (report
    paths and caches live outside the workspace).
    """
    return not element.startswith("/") and any(c in element for c in "*?[")


def _is_workspace_file(element: str, repo_root: Path | None) -> bool:
    """True when the operand names an existing workspace-relative file.

    412 round 13: the literal-target rule keys on this -- a model plan that
    kept a concrete source file (``bandit old.py``) scans out-of-diff code
    beside the changed set unless the operand is replaced. Nonexistent
    relative tokens (trivy's ``fs`` subcommand) stay alone, preserving tool
    plumbing. 412 round 15: an absolute operand under the workspace root is
    as much a scan target as its relative spelling; absolute paths outside
    the workspace (report paths, caches) stay alone, and config files ride
    option values (``-c``/``--config``), which are never operands.
    """
    if repo_root is None or not element or element.startswith("-"):
        return False
    try:
        if element.startswith("/"):
            absolute = Path(element)
            try:
                inside = absolute.resolve().is_relative_to(repo_root.resolve())
            except OSError:
                return False
            if not inside:
                return False
            # 412 round 43: an absolute glob under the workspace expands to
            # pre-existing files outside the diff exactly like its relative
            # spelling, which round 14 replaces -- expansion, not existence,
            # is what makes it a scan target.
            return absolute.exists() or any(c in element for c in "*?[")
        return (repo_root / element).exists()
    except OSError:
        return False


def _names_outside_workspace(element: str, repo_root: Path | None) -> bool:
    """True when the operand is an absolute path outside the workspace.

    412 round 41: the scoping guarantee is that the executed argv scans the
    changed set and nothing else. An absolute operand outside the workspace
    (``/etc/passwd``, ``/run/secrets/...``, an absolute glob) survives every
    replacement rule -- it is neither broad nor a workspace path -- and the
    tool reads it beside the changed set, its output riding into the report.
    Such a plan cannot be scoped as written, so it is refused and the card
    holds; the model re-plans. Config and report paths ride option values,
    which the value guards preserve and this check never reaches.
    """
    if repo_root is None or not element or not element.startswith("/"):
        return False
    try:
        return not Path(element).resolve().is_relative_to(repo_root.resolve())
    except OSError:
        return False


def _names_changed_set_element(element: str, files: list[str]) -> bool:
    """True when the operand names a changed path that is not on disk.

    412 round 15: a deleted path or a named phantom beyond the truncation cap
    is in the change set but fails an exists() check, so the literal-target
    rule alone leaves it in the argv (`bandit gone.py`). The workflow
    deliberately excluded that path; the operand is replaced by the changed
    set like any other scan target.
    """
    normalized = element[2:] if element.startswith("./") else element
    return normalized.rstrip("/") in {f.rstrip("/") for f in files}


def _is_workspace_dir(element: str, repo_root: Path | None) -> bool:
    """True when the operand names a directory inside the workspace.

    Option values that happen to name a directory (``--output build/``) can
    be caught too; a mistaken replacement makes the tool fail, which holds
    the card -- the safe side -- while an unreplaced directory scans the
    whole tree.
    """
    if repo_root is None or not element or element.startswith("-"):
        return False
    candidate = Path(element)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(repo_root)
        except ValueError:
            return False
    try:
        return (repo_root / candidate).is_dir()
    except OSError:
        return False


#: Path operands that mean "everything" — the model planning one of these is
#: planning a whole-workspace scan, and the changed files replace it.
_BROAD_SCAN_OPERANDS = frozenset({".", "./", "./...", "/", "/*", "./**", "**", "*", "~"})

#: 412 round 25: subcommand tokens of the common scanner CLIs, keyed by
#: program name. A subcommand is plumbing, not an operand, so the disk
#: heuristics never repoint it at the changed files.
_TOOL_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "trivy": frozenset({"fs", "image", "rootfs", "sbom", "scan", "config", "repository", "k8s"}),
    "gitleaks": frozenset({"detect", "report", "protect", "dir", "git"}),
    "semgrep": frozenset({"scan", "ci", "config", "sentry", "login"}),
    "gosec": frozenset(),
    "bandit": frozenset(),
    "pylint": frozenset(),
    "ruff": frozenset({"check", "format"}),
}

#: Exit codes that mean the tool ran, unless the tool declares otherwise: 0
#: (clean) and 1 (findings). 412 lets the ScanTool declare more -- pylint's
#: bitmask, trivy's 2 -- because per-tool exit semantics are exactly the
#: knowledge the model has and coordinare cannot.
DEFAULT_OK_EXIT_CODES = frozenset({0, 1})


async def _run_tool(
    argv: list[str], cwd: Path, tool: str, runner: Callable, budgets: SecurityBudgets,
    ok_exit_codes: frozenset[int] = DEFAULT_OK_EXIT_CODES,
) -> tuple[int | None, str, str, int]:
    """Run a scanner subprocess and return (exit_code, stdout, stderr, duration_ms)."""
    import time

    start = time.monotonic()
    try:
        exit_code, stdout, stderr = await runner(argv, cwd, budgets.scan_timeout_s)
    except asyncio.TimeoutError:
        raise ScannerUnavailable(tool, f"timed out after {budgets.scan_timeout_s}s")
    except FileNotFoundError:
        raise ScannerUnavailable(tool, "binary not found")
    duration = int((time.monotonic() - start) * 1000)
    if exit_code not in ok_exit_codes:
        raise ScannerUnavailable(tool, f"exited {exit_code} (declared ok exit codes: {sorted(ok_exit_codes)})")
    # 412: empty output with a declared-ok exit code is a clean quiet run,
    # not a crash. The reading's per-file coverage is what separates "read
    # nothing" from "read everything"; an empty stdout just reads as an
    # abstention there and holds the card honestly.
    return (exit_code, stdout, stderr, duration)


async def run_scan(
    files: list[str],
    repo_root: Path,
    *,
    tools: list[Any] | None = None,
    read: Callable | None = None,
    runner: Callable | None = None,
    budgets: SecurityBudgets,
) -> tuple[list[dict], list[ScanResult]]:
    """Run model-determined security tools over files, return (findings, results).

    Spec 366: The model determines what scanning applies to this repository by
    specifying the tools list. Each tool is a (name, build_fn, normalize_fn) tuple.
    build_fn(files, budgets) -> argv; normalize_fn(result) -> normalized_finding.

    If tools is None or empty, raises ScannerUnavailable to fail closed when no
    applicable scanning determined (abstention must not read as a pass).

    findings: list of dicts in the spec-022 schema. results: one ScanResult per tool
    that ran. Raises ScannerUnavailable on a missing tool, a crash, a timeout, an
    undeclared exit code, malformed output, or no applicable tools; the exception
    carries the ScanResults of the tools that completed before it.

    412: an empty file list raises NothingToScan (the workflow turns that into
    the explicit ``nothing_to_scan`` verdict); argv is scoped to the changed
    files via the ``{{files}}`` token or an append; a tool may declare extra
    ``ok_exit_codes`` (pylint's bitmask, trivy's 2); and empty stdout with a
    declared-ok exit code is a clean quiet run, not a crash.
    """
    if not files:
        raise NothingToScan("no changed files parsed from the diff")
    if runner is None:
        runner = default_runner
    if not tools:
        raise ScannerUnavailable("(no applicable tools)", "model determined no scanning applies to this repository")
    findings: list[dict] = []
    results: list[ScanResult] = []
    examined_any: set[str] = set()
    unexamined: list[str] = []
    # 412 round 24: coverage is trusted only for the files the run actually
    # scoped -- a malformed or hallucinated coverage row for an unrelated
    # path must not turn a zero-coverage run into a clean one.
    scoped = {normalise_tool_path(f) for f in files}
    for tool in tools:
        # 412: declared codes are EXTRA -- they union with the default {0, 1},
        # they do not replace it. A plan declaring only trivy's 2 must still
        # read trivy's clean 0 as a clean run.
        # 412 round 33: a model-declared ok code is an application code
        # (findings / none), never an OS process failure -- 126 (permission
        # denied), 127 (not found) or a signal death (>= 128) declared ok
        # would read a scanner that never ran as a clean pass.
        # 412 round 35: asyncio reports a signal kill as a NEGATIVE return
        # code (-9 for SIGKILL), so a negative declared code is the same
        # process failure in a different sign -- rejected before the run.
        extras = frozenset(getattr(tool, "ok_exit_codes", None) or ())
        os_failures = sorted(code for code in extras if code >= 126 or code < 0)
        if os_failures:
            raise ScannerUnavailable(tool.name, f"model declared OS process-failure exit code(s) as ok: {os_failures}")
        ok = DEFAULT_OK_EXIT_CODES | extras
        # 412 round 33: the argv build sits inside the try so a plan
        # rejected by the scoper (an unscoped inline operand) still carries
        # the results of the tools that already completed.
        try:
            argv = _scoped_argv(list(tool.argv), files, repo_root)
            exit_code, stdout, _stderr, duration = await _run_tool(argv, repo_root, tool.name, runner, budgets, ok)
        except ScannerUnavailable as exc:
            exc.results = list(results)
            raise
        # 366: the model reads the tool's own output. No per-tool normalizer,
        # and no JSON-shape assumption -- a scanner that prints a table is as
        # readable as one that prints JSON, and neither needs coordinare to
        # learn its format.
        reading = await read(tool.name, argv, stdout, files)
        # 412 round 26: tool output names paths the way the tool printed
        # them (``./src/app.py`` for ``src/app.py``) -- normalize exactly as
        # findings are normalized, or a valid run reads as zero coverage.
        examined_any.update(
            normalized for normalized in (normalise_tool_path(p) for p in reading.examined_paths())
            if normalized in scoped
        )
        unexamined.extend(f"{c.path} ({tool.name}: {c.reason})" for c in reading.unexamined())
        # The tool's identity is known exactly here, so stamp it. The gate used
        # to recover it by testing whether the description started with
        # "bandit:", which only works in a world with two scanners whose names
        # coordinare already knows -- the world #366 exists to leave.
        findings.extend({**f, "tool": tool.name} for f in reading.findings)
        results.append(ScanResult(
            tool=tool.name, command=shlex.join(argv), exit_code=exit_code,
            finding_count=len(reading.findings), duration_ms=duration,
        ))

    # 366: the defect this exists to stop. bandit on a Ruby repository exits 0
    # with zero findings and an errors block nobody read, so "examined nothing"
    # was indistinguishable from "found nothing". An abstention is a hold, not
    # a pass.
    if not examined_any:
        exc = ScannerUnavailable(
            ", ".join(t.name for t in tools),
            "examined none of the %d changed file(s): %s" % (len(files), "; ".join(unexamined[:5]) or "no tool reported reading any of them"),
        )
        exc.results = list(results)
        raise exc
    return (findings, results)


async def default_runner(argv: list[str], cwd: Path, timeout_s: int) -> tuple[int | None, str, str]:
    """Default async runner using asyncio.create_subprocess_exec."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_s,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return (proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"))
    except asyncio.TimeoutError:
        raise


__all__ = [
    "ScannerUnavailable",
    "NothingToScan",
    "FILES_TOKEN",
    "DEFAULT_OK_EXIT_CODES",
    "is_scannable_source",
    "has_scannable_source",
    "run_scan",
    "default_runner",
]
