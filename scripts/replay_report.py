#!/usr/bin/env python
"""Summarize environment-bootstrap acceptance runs from their own records (issue 376).

For each run directory: turns, the most parallel tool calls in one reply, invalid tool calls, schema corrections by
code (charged and uncharged), validation codes, per-session invalid-evidence strikes, finish reasons and the hold.
Per session (one agent identity slot), the same from its latest checkpoint. A rules section maps the prompt-rule
anchors in ``tests/fixtures/replay/rules.json`` to the codes they target and lists rules no given run ever hit.

Reads are read-only and bounded. Output is JSON plus a Markdown table; both are local diagnostics that may name the
run's own records, so they belong in a git-ignored directory, never the repository::

    scripts/replay_report.py RUN_DIR... --json OUT.json --md OUT.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from scripts.replay_extract import (
        MAX_READ_BYTES,
        checkpoint_head,
        correction_entries,
        read_bytes,
        read_json,
        slot,
        tool_error_kind,
    )
except ModuleNotFoundError:  # Run as a script: scripts/ is on sys.path and the repository root is not.
    from replay_extract import (  # type: ignore[no-redef]
        MAX_READ_BYTES,
        checkpoint_head,
        correction_entries,
        read_bytes,
        read_json,
        slot,
        tool_error_kind,
    )

DEFAULT_RULES = Path(__file__).resolve().parent.parent / 'tests' / 'fixtures' / 'replay' / 'rules.json'
MAX_FAILURE_OUTPUT = 256 * 1024
INVALID_KINDS = frozenset({'invalid_tool_arguments', 'unknown_tool', 'agent_evidence_page'})


def latest_checkpoints(run: Path) -> dict[str, Path]:
    """The newest checkpoint per identity slot: one native session's final state (checkpoints are cumulative)."""
    latest: dict[str, tuple[float, Path]] = {}
    for path in sorted((run / 'daemon-state' / 'bootstrap-agents').glob('*/v1-*.json')):
        identity = checkpoint_head(path)
        if identity is None:
            continue
        key = slot(identity)
        modified = path.stat().st_mtime
        if key not in latest or modified > latest[key][0]:
            latest[key] = (modified, path)
    return {key: path for key, (_modified, path) in latest.items()}


def session_summary(path: Path, phase_errors: dict[str, int], key: str) -> dict[str, Any]:
    data = read_json(path)
    session = json.loads(data['session_text'])
    max_parallel, invalid, tool_errors = 0, Counter(), 0
    for message in session.get('messages') or []:
        content = message.get('content') if isinstance(message.get('content'), list) else []
        if message.get('role') == 'assistant':
            max_parallel = max(max_parallel, sum(1 for block in content if block.get('type') == 'tool_use'))
            continue
        for block in content:
            if block.get('type') == 'tool_result' and block.get('is_error'):
                tool_errors += 1
                text = block['content'] if isinstance(block['content'], str) else json.dumps(block['content'])
                if (kind := tool_error_kind(text)) in INVALID_KINDS:
                    invalid[kind] += 1
    charged: Counter[str] = Counter()
    uncharged: Counter[str] = Counter()
    for entry in session.get('schema_correction_log') or []:
        for code in entry.get('codes') or []:
            for error in correction_entries(str(code)):
                (charged if entry.get('charged') else uncharged)[str(error.get('code'))] += 1
    identity = data['identity']
    return {'slot': key[:16], 'phase': identity.get('phase'), 'role': identity.get('role'),
            'turns': session.get('turns', 0), 'max_parallel_tool_calls': max_parallel,
            'invalid_tool_calls': sum(invalid.values()), 'invalid_by_kind': dict(invalid), 'tool_errors': tool_errors,
            'corrections_charged': dict(charged), 'corrections_uncharged': dict(uncharged),
            'evidence_strikes': phase_errors.get(key, 0), 'stop_reason': session.get('stop_reason'),
            'terminal_status': data.get('terminal_status'), 'hold_code': data.get('hold_code')}


def failure_outputs(journal: dict[str, Any]) -> list[str]:
    """The output of every failed execution result, bounded per result."""
    outputs = []
    for rejected in journal.get('rejected') or []:
        for result in (rejected.get('report') or {}).get('results') or []:
            if result.get('status') not in {'pass', 'passed'} and isinstance(result.get('output'), str):
                outputs.append(result['output'][-MAX_FAILURE_OUTPUT:])
    return outputs


def run_summary(run: Path) -> dict[str, Any]:
    journal_path = run / 'nare' / 'environment-attempt.json'
    journal = read_json(journal_path) if journal_path.exists() else {}
    results_path = run / 'results.json'
    results = read_json(results_path) if results_path.exists() else []
    result = results[0] if isinstance(results, list) and results else {}
    diagnostics = journal.get('diagnostics') or {}
    phase_errors = journal.get('agent_evidence_phase_errors') or {}
    sessions = [session_summary(path, phase_errors, key) for key, path in sorted(latest_checkpoints(run).items())]
    codes: Counter[str] = Counter(diagnostics.get('validation_codes') or journal.get('validation_codes') or [])
    corrections_charged: Counter[str] = Counter()
    corrections_uncharged: Counter[str] = Counter()
    invalid: Counter[str] = Counter()
    for session in sessions:
        corrections_charged.update(session['corrections_charged'])
        corrections_uncharged.update(session['corrections_uncharged'])
        invalid.update(session['invalid_by_kind'])
    findings = Counter({f'finding:{category}': count for source in ('finding_categories', 'scope_finding_categories')
                        for category, count in (diagnostics.get(source) or {}).items()})
    hold = {'status': result.get('status') or journal.get('status'), 'stage': result.get('stage') or diagnostics.get('stage'),
            'code': journal.get('last_error') or diagnostics.get('error_code') or result.get('reason_code'),
            'detail': diagnostics.get('error_detail'), 'delta_fallback': diagnostics.get('delta_fallback_code')}
    signals = codes + corrections_charged + corrections_uncharged + invalid + findings
    for code in (hold['code'], hold['delta_fallback']):
        if code:
            signals[str(code)] += 1
    return {
        'run': run.name, 'hold': hold, 'calls': journal.get('calls'), 'sessions_count': len(sessions),
        'turns': sum(session['turns'] for session in sessions),
        'max_parallel_tool_calls': max((session['max_parallel_tool_calls'] for session in sessions), default=0),
        'invalid_tool_calls': sum(invalid.values()), 'invalid_by_kind': dict(invalid),
        'corrections_charged': sum(corrections_charged.values()), 'corrections_uncharged': sum(corrections_uncharged.values()),
        'correction_codes_charged': dict(corrections_charged), 'correction_codes_uncharged': dict(corrections_uncharged),
        'validation_codes': dict(codes), 'finish_reasons': journal.get('finish_reasons') or diagnostics.get('finish_reasons'),
        'evidence_errors': journal.get('agent_evidence_errors'),
        'evidence_strikes_max': max(phase_errors.values(), default=0), 'finding_categories': dict(findings),
        'signals': dict(signals), 'failure_outputs': len(failure_outputs(journal)), 'sessions': sessions,
        '_failures': failure_outputs(journal),
    }


def rule_hits(rules: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for rule in rules:
        targets, patterns = rule.get('targets') or [], rule.get('failure_patterns') or []
        per_run = {}
        for run in runs:
            hits = sum(run['signals'].get(target, 0) for target in targets)
            hits += sum(1 for output in run['_failures'] for pattern in patterns if re.search(pattern, output))
            per_run[run['run']] = hits
        rows.append({'anchor': rule['anchor'], 'file': rule['file'], 'targets': targets,
                     'failure_patterns': patterns, 'measurable': bool(targets or patterns),
                     'hits': sum(per_run.values()), 'per_run': per_run})
    return rows


def _codes(counts: dict[str, int]) -> str:
    return ', '.join(f'{code} {count}' for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))) or '-'


def markdown(runs: list[dict[str, Any]], rules: list[dict[str, Any]], unreadable: list[str]) -> str:
    lines = ['# Bootstrap replay report', '', '## Runs', '',
             '| run | hold | stage | calls | sessions | turns | max parallel | invalid calls | corrections charged / '
             'uncharged | evidence strikes (max per session) | finish reasons |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for run in runs:
        hold = run['hold']
        lines.append(f"| {run['run']} | {hold['code'] or hold['status']} | {hold['stage']} | {run['calls']} | "
                     f"{run['sessions_count']} | {run['turns']} | {run['max_parallel_tool_calls']} | "
                     f"{run['invalid_tool_calls']} | {run['corrections_charged']} / {run['corrections_uncharged']} | "
                     f"{run['evidence_strikes_max']} | {_codes(run['finish_reasons'] or {})} |")
    if unreadable:
        lines += ['', 'Unreadable (not mounted or missing): ' + ', '.join(unreadable)]
    for run in runs:
        lines += ['', f"## {run['run']}", '', f"Validation codes: {_codes(run['validation_codes'])}",
                  f"Corrections charged: {_codes(run['correction_codes_charged'])}",
                  f"Corrections uncharged: {_codes(run['correction_codes_uncharged'])}", '',
                  '| session | phase | role | turns | max parallel | invalid calls | corrections charged | '
                  'corrections uncharged | evidence strikes | stop | terminal |',
                  '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
        for session in run['sessions']:
            terminal = session['terminal_status'] or '-'
            if session['hold_code']:
                terminal += f" ({session['hold_code']})"
            lines.append(f"| {session['slot']} | {session['phase']} | {session['role']} | {session['turns']} | "
                         f"{session['max_parallel_tool_calls']} | {session['invalid_tool_calls']} | "
                         f"{_codes(session['corrections_charged'])} | {_codes(session['corrections_uncharged'])} | "
                         f"{session['evidence_strikes']} | {session['stop_reason']} | {terminal} |")
    lines += ['', '## Prompt rules', '', '| anchor | targets | hits |', '| --- | --- | --- |']
    for rule in rules:
        targets = ', '.join(rule['targets'] + [f'output /{pattern}/' for pattern in rule['failure_patterns']]) or '-'
        lines.append(f"| {rule['anchor']} | {targets} | {rule['hits'] if rule['measurable'] else 'not measurable'} |")
    zero = [rule['anchor'] for rule in rules if rule['measurable'] and not rule['hits']]
    unmeasured = [rule['anchor'] for rule in rules if not rule['measurable']]
    lines += ['', f"Rules with zero hits across {len(runs)} runs: {', '.join(zero) or 'none'}",
              f"Rules no run artifact can measure: {', '.join(unmeasured) or 'none'}", '']
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('run_dirs', nargs='+')
    parser.add_argument('--rules', default=str(DEFAULT_RULES))
    parser.add_argument('--json', dest='json_out', help='write the JSON report here')
    parser.add_argument('--md', dest='md_out', help='write the Markdown report here')
    args = parser.parse_args(argv)
    rules = json.loads(read_bytes(Path(args.rules), MAX_READ_BYTES))['rules']
    runs, unreadable = [], []
    for name in args.run_dirs:
        run = Path(name)
        if not (run / 'daemon-state').is_dir():
            unreadable.append(run.name)
            continue
        runs.append(run_summary(run))
    hits = rule_hits(rules, runs)
    report = {'runs': [{key: value for key, value in run.items() if key != '_failures'} for run in runs],
              'unreadable': unreadable, 'rules': hits,
              'zero_hit_rules': [rule['anchor'] for rule in hits if rule['measurable'] and not rule['hits']],
              'unmeasurable_rules': [rule['anchor'] for rule in hits if not rule['measurable']]}
    text = markdown(runs, hits, unreadable)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1) + '\n', encoding='utf-8')
    if args.md_out:
        Path(args.md_out).write_text(text, encoding='utf-8')
    if not args.json_out and not args.md_out:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
