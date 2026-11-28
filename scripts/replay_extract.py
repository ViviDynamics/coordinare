#!/usr/bin/env python
"""Extract one offline replay fixture from an environment-bootstrap acceptance run (issue 376).

Harness only: it reads a run directory and writes a JSON fixture; nothing in coordinare imports it. Reads are
read-only and bounded (one file at most ``MAX_READ_BYTES``), and the run directory is never written.

A run directory holds three records of a bootstrap attempt:

* ``daemon-state/bootstrap-agents/<attempt_id>/v1-<sha256>.json``: immutable agent checkpoints, one per turn. Their
  ``session_text`` is the whole native session, with tool results, correction messages and ``schema_correction_log``.
  These are the source of record.
* ``daemon-transcripts/_conductor/system/<call>.jsonl``: one file per model call, the reply's text and tool calls at
  full length (the prompt is truncated).
* ``nare/environment-attempt.json``: the attempt journal (rejected manifests, executable parts, validation codes).

The repository is public, so a committed fixture is redacted: record ids, ``required``, ``scope`` and ``depends_on``
are kept; each distinct argv, cwd or env token becomes ``tNN`` (the same value always maps to the same token, so
duplicate-content rules still fire); each path becomes ``src/fNN``; free text becomes ``<text:sha8>``; env names
become ``ENV_NN``. Tool outputs and source text are never copied. Before writing, the serialized fixture is scanned
against a denylist (home paths, hostnames listed in an untracked local file, URLs outside a small allowlist,
high-entropy strings, email addresses, and the ``serving_route`` and ``base_cache_dir`` fields), and the fixture is
refused on any match.

``--local`` writes an unredacted copy instead, and only into a git-ignored ``.agents/state/replay-corpus/``
directory.

Examples::

    scripts/replay_extract.py RUN_DIR --case first-reply --transcript acb5d23a --out tests/fixtures/replay/
    scripts/replay_extract.py RUN_DIR --case one-refusal --checkpoint e4c2ff31/v1-6ec652ca --turns 21-25 \
        --journal-prior -1 --out tests/fixtures/replay/
    scripts/replay_extract.py RUN_DIR --case pinned-pair --journal-parts --out tests/fixtures/replay/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

FORMAT = 1
REDACTION_VERSION = 1
MAX_READ_BYTES = 64 * 1024 * 1024
CHECKPOINT_HEAD_BYTES = 16 * 1024

# Hosts a committed fixture may name in a URL. Everything else is refused.
URL_ALLOWLIST = frozenset({'example.com', 'example.org', 'example.net'})
FORBIDDEN_FIELDS = ('serving_route', 'base_cache_dir')
# Untracked, one hostname (or any literal) per line; kept out of the repository on purpose.
DENYLIST_FILE = Path('.agents/state/replay-denylist.txt')
LOCAL_CORPUS = ('.agents', 'state', 'replay-corpus')

# Keys whose string values are record ids or closed vocabulary, kept as they are.
ID_KEYS = frozenset({'id', 'finding_id', 'command_id', 'criterion_id', 'duplicate_of', 'service'})
ID_LIST_KEYS = frozenset({'depends_on', 'checks', 'affected_ids', 'scope', 'command_ids', 'criterion_ids',
                          'unavailable', 'allowed_ids', 'required_ids', 'readiness_ids', 'added_ids'})
ENUM_KEYS = frozenset({'category', 'gap_kind', 'operation', 'verdict', 'status', 'applicability', 'disposition',
                       'code', 'kind', 'tool', 'name', 'role', 'phase'})
PATH_KEYS = frozenset({'source', 'path'})
PATH_LIST_KEYS = frozenset({'sources', 'paths'})
TOKEN_LIST_KEYS = frozenset({'argv'})
EXECUTION_PATH_KEYS = frozenset({'cwd'})
ID_PATTERN = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
ENUM_PATTERN = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
ROOT = re.compile(r'^(\{(?:cache|repo|scratch)\}|/)')
FENCED_JSON = re.compile(r'```(?:json)?\s*(\{.*\})\s*```', re.DOTALL)
CORRECTION_PREFIX = 'Your answer did not satisfy'


class RefusedFixture(Exception):  # noqa: N818 - the extractor's own refusal, not a programming error
    """The serialized fixture matched the denylist."""


class Redactor:
    """Consistent replacement maps for one fixture: the same value always gets the same stand-in."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.paths: dict[str, str] = {}
        self.env_names: dict[str, str] = {}
        self.texts = 0

    def token(self, value: str) -> str:
        return self.tokens.setdefault(value, f't{len(self.tokens) + 1:02d}')

    def path(self, value: str) -> str:
        return self.paths.setdefault(value, f'src/f{len(self.paths) + 1:02d}')

    def env_name(self, value: str) -> str:
        return self.env_names.setdefault(value, f'ENV_{len(self.env_names) + 1:02d}')

    def text(self, value: str) -> str:
        self.texts += 1
        return f'<text:{hashlib.sha256(value.encode()).hexdigest()[:8]}>'

    def execution_path(self, value: str) -> str:
        """Keep the daemon's root placeholder so the redacted command still validates; tokenize the rest."""
        match = ROOT.match(value)
        if match is None:
            return self.token(value)
        rest = value[match.end():].lstrip('/')
        root = match.group(1).rstrip('/')
        return root if not rest else f'{root}/{self.token(value)}'

    def identifier(self, value: str) -> str:
        return value if ID_PATTERN.fullmatch(value) else self.text(value)

    def value(self, value: Any, key: str | None = None) -> Any:
        """Redact ``value`` by the field it sits under."""
        if isinstance(value, dict):
            if key == 'env':
                return {self.env_name(str(name)): self.token(str(item)) for name, item in value.items()}
            if key == 'criterion_checks':
                return {self.identifier(str(name)): [self.identifier(str(item)) for item in items]
                        for name, items in value.items() if isinstance(items, list)}
            return {(name if ENUM_PATTERN.fullmatch(str(name)) else self.text(str(name))): self.value(item, str(name))
                    for name, item in value.items()}
        if isinstance(value, list):
            if key in TOKEN_LIST_KEYS:
                return [self.token(str(item)) for item in value]
            if key in PATH_LIST_KEYS:
                return [self.path(str(item)) if isinstance(item, str) else self.value(item) for item in value]
            if key in ID_LIST_KEYS:
                return [self.identifier(item) if isinstance(item, str) else self.value(item) for item in value]
            if key == 'path':
                return [self.execution_path(str(item)) for item in value]
            if key == 'pairs':
                return [[self.identifier(str(item)) for item in pair] for pair in value if isinstance(pair, list)]
            return [self.value(item) for item in value]
        if not isinstance(value, str):
            return value
        if key in ID_KEYS:
            return self.identifier(value)
        if key in ENUM_KEYS and ENUM_PATTERN.fullmatch(value):
            return value
        if key in EXECUTION_PATH_KEYS:
            return self.execution_path(value)
        if key in PATH_KEYS:
            return self.path(value)
        return self.text(value)

    def summary(self) -> dict[str, int]:
        return {'version': REDACTION_VERSION, 'tokens': len(self.tokens), 'paths': len(self.paths),
                'env_names': len(self.env_names), 'texts': self.texts}


class Verbatim(Redactor):
    """The ``--local`` copy: the same walk, nothing replaced."""

    def value(self, value: Any, key: str | None = None) -> Any:
        return value

    def summary(self) -> dict[str, int]:
        return {'version': 0}


# ----- denylist -----

def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(count / len(text) * math.log2(count / len(text)) for count in counts.values())


def local_denylist(root: Path, *, relative: bool = True) -> list[str]:
    """Literals from the untracked denylist file, if it exists (one per line, # comments)."""
    path = root / DENYLIST_FILE if relative else root
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith('#')]


def scan(text: str, *, denied: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Every denylist match in ``text``, as short descriptions that never repeat the matched value in full."""
    problems: list[str] = []
    for marker in ('/Users/', '/home/'):
        if marker in text:
            problems.append(f'home path {marker!r}')
    lowered = text.lower()
    problems.extend(f'denied literal #{index}' for index, literal in enumerate(denied, 1) if literal.lower() in lowered)
    for match in re.finditer(r'[A-Za-z][A-Za-z0-9+.-]*://([^/\s"\'<>:@]+)', text):
        host = match.group(1).lower()
        if host not in URL_ALLOWLIST:
            problems.append(f'url host outside allowlist ({len(host)} chars)')
    if re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text):
        problems.append('email address')
    problems.extend(f'forbidden field {name}' for name in FORBIDDEN_FIELDS if f'"{name}"' in text)
    for candidate in re.findall(r'[A-Za-z0-9+/_=-]{24,}', text):
        if re.fullmatch(r'[0-9a-f]+', candidate) or candidate.count('-') + candidate.count('_') > len(candidate) // 6:
            continue  # Digests, and id-like words joined by separators.
        if re.search(r'[a-z]', candidate) and re.search(r'[A-Z]', candidate) and re.search(r'[0-9]', candidate) \
                and _entropy(candidate) >= 4.0:
            problems.append(f'high-entropy string ({len(candidate)} chars)')
    return problems


def write_fixture(fixture: dict[str, Any], out: Path, *, denied: list[str], local: bool) -> Path:
    text = json.dumps(fixture, indent=1, sort_keys=False) + '\n'
    if local:
        _require_local_corpus(out)
    elif problems := scan(text, denied=denied):
        raise RefusedFixture('fixture refused by the denylist scan: ' + '; '.join(sorted(set(problems))))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    return out


def _require_local_corpus(out: Path) -> None:
    """An unredacted copy goes only under a git-ignored .agents/state/replay-corpus/ directory."""
    resolved = out.resolve()
    parts = resolved.parts
    inside = any(parts[index:index + len(LOCAL_CORPUS)] == LOCAL_CORPUS for index in range(len(parts)))
    git = shutil.which('git')
    ignored = False
    if inside and git is not None:
        corpus = Path(*parts[:next(index for index in range(len(parts))
                                   if parts[index:index + len(LOCAL_CORPUS)] == LOCAL_CORPUS) + len(LOCAL_CORPUS)])
        repo = corpus.parent.parent.parent
        result = subprocess.run([git, '-C', str(repo), 'check-ignore', '-q', str(resolved)],
                                check=False, capture_output=True)
        ignored = result.returncode == 0
    if not (inside and ignored):
        raise RefusedFixture('--local writes only under a git-ignored .agents/state/replay-corpus/ directory')


# ----- bounded reads -----

def read_bytes(path: Path, limit: int = MAX_READ_BYTES) -> bytes:
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        message = f'{path.name} exceeds the {limit} byte read bound'
        raise ValueError(message)
    return content


def read_json(path: Path) -> Any:
    return json.loads(read_bytes(path))


def checkpoint_head(path: Path) -> dict[str, Any] | None:
    """The identity from a checkpoint's first bytes (fields are written in declaration order, identity first)."""
    with path.open('rb') as stream:
        head = stream.read(CHECKPOINT_HEAD_BYTES).decode('utf-8', 'replace')
    start = head.find('"identity":')
    if start < 0:
        return None
    try:
        identity, _end = json.JSONDecoder().raw_decode(head, start + len('"identity":'))
    except ValueError:
        return None
    return identity if isinstance(identity, dict) else None


def slot(identity: dict[str, Any]) -> str:
    """The session slot the attempt journal keys per-session accounting by (nare_runtime._Execution.slot)."""
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def find_checkpoint(run: Path, reference: str) -> Path:
    """``<attempt prefix>/<key prefix>`` names one checkpoint file."""
    attempt_prefix, _, key_prefix = reference.partition('/')
    root = run / 'daemon-state' / 'bootstrap-agents'
    matches = [path for attempt in sorted(root.iterdir()) if attempt.name.startswith(attempt_prefix)
               for path in sorted(attempt.glob('v1-*.json')) if path.name.removeprefix('v1-').startswith(
                   key_prefix.removeprefix('v1-'))]
    if len(matches) != 1:
        message = f'checkpoint reference {reference!r} matches {len(matches)} files'
        raise ValueError(message)
    return matches[0]


def find_transcript(run: Path, prefix: str) -> Path:
    matches = sorted((run / 'daemon-transcripts' / '_conductor' / 'system').glob(f'{prefix}*.jsonl'))
    if len(matches) != 1:
        message = f'transcript prefix {prefix!r} matches {len(matches)} files'
        raise ValueError(message)
    return matches[0]


def load_checkpoint(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    data = read_json(path)
    return data, json.loads(data['session_text'])


# ----- refusals -----

def correction_entries(code_text: str) -> list[dict[str, Any]]:
    """The structured errors one correction code string holds (a JSON object, or ``field: {json}``)."""
    start = code_text.find('{')
    if start < 0:
        return [{'code': _schema_kind(code_text)}]
    try:
        value, _end = json.JSONDecoder().raw_decode(code_text, start)
    except ValueError:
        return [{'code': _schema_kind(code_text)}]
    return [value] if isinstance(value, dict) else [{'code': _schema_kind(code_text)}]


def _schema_kind(text: str) -> str:
    """A schema correction's prose, reduced to a stable code."""
    for pattern, code in ((r'is incomplete', 'json_incomplete'), (r'no JSON document', 'json_missing'),
                          (r'two JSON documents', 'json_repeated'), (r'unexpected property', 'schema_unexpected_property'),
                          (r'cites files not yet read', 'schema_cites_unread'), (r'matches no anyOf', 'schema_anyof'),
                          (r'is a required property|missing required', 'schema_required_missing')):
        if re.search(pattern, text):
            return code
    return 'schema_other'


def guidance_ids(errors: list[dict[str, Any]], known: set[str]) -> list[str]:
    """The record ids a refusal names: its id lists, and id-shaped words of its guidance that are known ids."""
    named: list[str] = []
    for error in errors:
        for key in ('command_id', 'command_ids', 'unavailable', 'pairs', 'missing', 'required_ids'):
            value = error.get(key)
            items = value if isinstance(value, list) else [value] if value is not None else []
            for item in items:
                named.extend(str(part) for part in (item if isinstance(item, list) else [item]))
        guidance = str(error.get('guidance') or '')
        named.extend(word for word in re.findall(r'[A-Za-z0-9_.:-]+', guidance) if word in known)
    return sorted({item for item in named if ID_PATTERN.fullmatch(item)})


def record_ids(data: Any) -> set[str]:
    """Every record id under ``data``: ids of requirements, commands, services and criterion mappings."""
    found: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key == 'id' and isinstance(value, str):
                found.add(value)
            elif key == 'criterion_checks' and isinstance(value, dict):
                found.update(value)
            found |= record_ids(value)
    elif isinstance(data, list):
        for item in data:
            found |= record_ids(item)
    return found


# ----- steps -----

def candidate_of(text: str) -> dict[str, Any] | None:
    """The JSON envelope an assistant reply holds, fenced or bare."""
    match = FENCED_JSON.search(text)
    body = match.group(1) if match else text[text.find('{'):] if '{' in text else ''
    try:
        value, _end = json.JSONDecoder().raw_decode(body.strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def checkpoint_steps(path: Path, turns: set[int] | None, known: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Each selected assistant turn of one session, with the correction that answered it."""
    data, session = load_checkpoint(path)
    messages = session['messages']
    log = list(session.get('schema_correction_log') or [])
    corrections = [index for index, message in enumerate(messages) if message['role'] == 'user'
                   and isinstance(message['content'], list)
                   and any(block.get('type') == 'text' and str(block.get('text', '')).startswith(CORRECTION_PREFIX)
                           for block in message['content'])]
    # The log has one entry per correction, in order; a compaction can elide earlier messages but never the log, so
    # the two are aligned from their ends.
    shared = min(len(log), len(corrections))
    log_for = dict(zip(corrections[len(corrections) - shared:], log[len(log) - shared:], strict=True))
    replies: list[tuple[int, int, dict[str, Any]]] = []
    turn = 0
    for index, message in enumerate(messages):
        if message['role'] == 'assistant':
            turn += 1
            replies.append((turn, index, message))
    for _turn, _index, message in replies:
        for block in message['content'] if isinstance(message['content'], list) else []:
            if block.get('type') == 'text' and (candidate := candidate_of(str(block['text']))) is not None:
                known |= record_ids(candidate)
    steps = []
    key = f"checkpoint:{path.parent.name[:8]}/{path.name[3:19]}"
    for number, index, message in replies:
        if turns is not None and number not in turns:
            continue
        reply: dict[str, Any] = {}
        content = message['content'] if isinstance(message['content'], list) else []
        texts = [str(block['text']) for block in content if block.get('type') == 'text']
        calls = [{'name': block['name'], 'args': block.get('input') or {}, 'id': block.get('id')}
                 for block in content if block.get('type') == 'tool_use']
        if texts and (candidate := candidate_of(texts[-1])) is not None:
            reply['candidate'] = candidate
        elif texts:
            reply['text'] = texts[-1]
        if calls:
            reply['tool_calls'] = calls
        following = next((position for position in corrections if position > index), None)
        entry = log_for.get(following) if following == index + 1 else None
        errors = [error for code in (entry or {}).get('codes', []) for error in correction_entries(str(code))]
        tool_errors = _tool_refusals(messages, index, calls)
        steps.append({'call': f'{key}#m{index}', 'session': slot(data['identity'])[:16], 'turn': number,
                      'reply': reply, 'recorded': {
                          'refusal_codes': [str(error.get('code')) for error in errors] + tool_errors,
                          'guidance_ids': guidance_ids(errors, known), 'charged': bool(entry and entry.get('charged'))
                                                                                 or bool(tool_errors)}})
    return steps, data


def _tool_refusals(messages: list[dict[str, Any]], index: int, calls: list[dict[str, Any]]) -> list[str]:
    """Refusal kinds of this reply's tool calls, read from the tool results that follow it."""
    if not calls or index + 1 >= len(messages):
        return []
    ids = {call['id'] for call in calls}
    kinds = []
    for block in messages[index + 1]['content'] if isinstance(messages[index + 1]['content'], list) else []:
        if block.get('type') == 'tool_result' and block.get('tool_use_id') in ids and block.get('is_error'):
            text = block['content'] if isinstance(block['content'], str) else json.dumps(block['content'])
            kinds.append(tool_error_kind(text))
    return kinds


def tool_error_kind(text: str) -> str:
    if re.search(r'unknown parameter|takes no parameters|missing required|validation error', text, re.IGNORECASE):
        return 'invalid_tool_arguments'
    if re.search(r'unknown tool|not an allowed tool', text, re.IGNORECASE):
        return 'unknown_tool'
    if 'page' in text.lower():
        return 'agent_evidence_page'
    return 'tool_error'


def transcript_step(run: Path, prefix: str, known: set[str]) -> dict[str, Any]:
    """One model call from its transcript, joined to the checkpoint that holds its tool results."""
    path = find_transcript(run, prefix)
    rows = [json.loads(line) for line in read_bytes(path).decode('utf-8').splitlines() if line.strip()]
    header = next(row for row in rows if row.get('seq') == 0)
    native = header.get('conductor', {}).get('parameters', {}).get('native_request') or {}
    calls = [{'name': row['tool_name'], 'args': row.get('input') or {}, 'id': row.get('call_id')}
             for row in rows if row.get('type') == 'tool_call']
    texts = [row['text'] for row in rows if row.get('type') == 'assistant_text']
    reply: dict[str, Any] = {}
    if texts and (candidate := candidate_of(texts[-1])) is not None:
        reply['candidate'] = candidate
    elif texts:
        reply['text'] = texts[-1]
    if calls:
        reply['tool_calls'] = calls
    step: dict[str, Any] = {'call': f'transcript:{path.stem}', 'session': None, 'turn': None, 'reply': reply,
                            'kind': header.get('conductor', {}).get('kind'),
                            'request': {'tools': [tool.get('name') for tool in native.get('tools') or []],
                                        'tool_choice': native.get('tool_choice')},
                            'recorded': {'refusal_codes': [], 'guidance_ids': [], 'charged': False}}
    if calls and (joined := _join_calls(run, calls)) is not None:
        checkpoint, session, index = joined
        step['session'] = slot(checkpoint['identity'])[:16]
        step['turn'] = sum(1 for message in session['messages'][:index + 1] if message['role'] == 'assistant')
        errors = _tool_refusals(session['messages'], index, calls)
        step['recorded'] = {'refusal_codes': errors, 'guidance_ids': [], 'charged': bool(errors)}
    return step


def _join_calls(run: Path, calls: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], int] | None:
    """The latest checkpoint whose session holds the first call id, and the index of the reply that made it."""
    needle = str(calls[0]['id']).encode()
    best: tuple[float, Path] | None = None
    for path in (run / 'daemon-state' / 'bootstrap-agents').glob('*/v1-*.json'):
        if needle in read_bytes(path) and (best is None or path.stat().st_mtime > best[0]):
            best = (path.stat().st_mtime, path)
    if best is None:
        return None
    checkpoint, session = load_checkpoint(best[1])
    for index, message in enumerate(session['messages']):
        if message['role'] == 'assistant' and isinstance(message['content'], list) and any(
                block.get('id') == calls[0]['id'] for block in message['content']):
            return checkpoint, session, index
    return None


# ----- fixture -----

def redact_step(step: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    reply = step['reply']
    redacted: dict[str, Any] = {}
    if 'candidate' in reply:
        redacted['candidate'] = redactor.value(reply['candidate'])
    if 'text' in reply:
        redacted['text'] = redactor.value(reply['text'])
    if 'tool_calls' in reply:
        redacted['tool_calls'] = [{'name': call['name'], 'args': {
            key if ENUM_PATTERN.fullmatch(str(key)) else redactor.text(str(key)): redactor.value(value)
            for key, value in (call['args'] or {}).items()}} for call in reply['tool_calls']]
    return {**step, 'reply': redacted}


def build_fixture(args: argparse.Namespace, redactor: Redactor) -> dict[str, Any]:
    run = Path(args.run_dir)
    journal_path = run / 'nare' / 'environment-attempt.json'
    journal = read_json(journal_path) if journal_path.exists() else {}
    results_path = run / 'results.json'
    results = read_json(results_path) if results_path.exists() else []
    result = results[0] if isinstance(results, list) and results else {}
    known: set[str] = set()
    state: dict[str, Any] = {}
    if args.journal_prior is not None:
        prior = journal['rejected'][args.journal_prior]['manifest']
        known |= record_ids(prior)
        state['prior_manifest'] = {key: prior.get(key) for key in
                                   ('install', 'checks', 'requirements', 'services', 'criterion_checks')}
    steps: list[dict[str, Any]] = []
    if args.journal_parts:
        parts = journal['executable_parts']
        state['merge'] = {'global': 'global', 'groups': sorted(parts['groups'])}
        for owner, entry in [('global', parts['global']['global']),
                             *sorted((prefix, value) for prefix, value in parts['groups'].items())]:
            data = entry['data']
            steps.append({'call': f"journal:executable_parts/{entry['key'][:16]}", 'session': owner, 'turn': None,
                          'reply': {'candidate': data}, 'recorded': {
                              'refusal_codes': [], 'guidance_ids': [], 'charged': False}})
        codes = list(journal.get('diagnostics', {}).get('validation_codes') or [])
        state['recorded_merge_codes'] = codes
    if args.checkpoint:
        selected = _turns(args.turns)
        checkpoint_path = find_checkpoint(run, args.checkpoint)
        found, checkpoint = checkpoint_steps(checkpoint_path, selected, known)
        state.setdefault('phase', checkpoint['identity']['phase'])
        state.setdefault('role', checkpoint['identity']['role'])
        steps.extend(found)
    for prefix in args.transcript or []:
        step = transcript_step(run, prefix, known)
        state.setdefault('conductor_kind', step.pop('kind'))
        steps.append(step)
    if args.evidence_strikes:
        phase_errors = journal.get('agent_evidence_phase_errors') or {}
        for step in steps:
            matches = [count for key, count in phase_errors.items() if step['session'] and key.startswith(step['session'])]
            if matches:
                step['recorded']['evidence_strikes'] = matches[0]
    redacted_state = {key: (redactor.value(value) if key == 'prior_manifest' else value) for key, value in state.items()}
    redacted_steps = [redact_step(step, redactor) for step in steps]
    return {
        'format': FORMAT,
        'run': run.name,
        'head': args.head,
        'hold': {'code': journal.get('last_error') or result.get('reason_code'), 'stage': result.get('stage')},
        'seam': args.seam,
        'redaction': redactor.summary(),
        'state': redacted_state,
        'steps': redacted_steps,
    }


def _turns(text: str | None) -> set[int] | None:
    if not text:
        return None
    selected: set[int] = set()
    for part in text.split(','):
        low, _, high = part.partition('-')
        selected.update(range(int(low), int(high or low) + 1))
    return selected


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('run_dir')
    parser.add_argument('--case', required=True, help='fixture name: <out>/<run>/<case>.json')
    parser.add_argument('--out', default='tests/fixtures/replay', help='fixture root (default: tests/fixtures/replay)')
    parser.add_argument('--seam', default='nare_transport', help='where the replay feeds the harness')
    parser.add_argument('--head', default='unknown', help='the coordinare head the run used')
    parser.add_argument('--checkpoint', help='<attempt prefix>/<checkpoint key prefix>: replay that session')
    parser.add_argument('--turns', help='session turns to keep, e.g. 21-25 or 3,5')
    parser.add_argument('--transcript', action='append', help='transcript id prefix (repeatable)')
    parser.add_argument('--journal-prior', type=int, help='index into the journal rejected[] for the prior manifest')
    parser.add_argument('--journal-parts', action='store_true', help='steps from the journal executable parts')
    parser.add_argument('--evidence-strikes', action='store_true', help='record per-session invalid-call strikes')
    parser.add_argument('--denylist', help='another untracked file of denied literals, one per line')
    parser.add_argument('--local', action='store_true', help='unredacted copy, only under .agents/state/replay-corpus/')
    args = parser.parse_args(argv)
    redactor: Redactor = Verbatim() if args.local else Redactor()
    fixture = build_fixture(args, redactor)
    out = Path(args.out) / fixture['run'] / f'{args.case}.json'
    try:
        denied = local_denylist(_repo_root()) + local_denylist(Path.cwd())
        if args.denylist:
            denied += local_denylist(Path(args.denylist), relative=False)
        written = write_fixture(fixture, out, denied=denied, local=args.local)
    except RefusedFixture as refused:
        sys.stderr.write(f'{refused}\n')
        return 3
    sys.stdout.write(f'{written} ({len(fixture["steps"])} steps)\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
