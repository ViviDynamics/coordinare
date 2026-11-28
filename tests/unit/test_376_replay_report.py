"""The replay report summarizes a run from its own records, and its rule map names real prompt rules."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from scripts import replay_report as report

from tests.unit.test_376_replay_extract import _write_run

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_rule_anchor_is_a_constant_in_its_file() -> None:
    rules = json.loads(report.DEFAULT_RULES.read_text(encoding='utf-8'))['rules']
    assert len({rule['anchor'] for rule in rules}) == len(rules)
    for rule in rules:
        tree = ast.parse((REPO_ROOT / rule['file']).read_text(encoding='utf-8'))
        names = {target.id for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets if isinstance(target, ast.Name)}
        assert rule['anchor'] in names, rule


def test_a_run_is_summarized_per_session_and_rules_without_hits_are_named(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    rules = tmp_path / 'rules.json'
    rules.write_text(json.dumps({'rules': [
        {'anchor': 'HIT', 'file': 'x', 'targets': ['clean_dependency_install_only']},
        {'anchor': 'MISS', 'file': 'x', 'targets': ['system_package_install']},
        {'anchor': 'TOOLS', 'file': 'x', 'targets': ['invalid_tool_arguments']},
        {'anchor': 'NONE', 'file': 'x', 'targets': []}]}))
    out_json, out_md = tmp_path / 'r.json', tmp_path / 'r.md'
    assert report.main([str(run), str(tmp_path / 'not-mounted'), '--rules', str(rules), '--json', str(out_json),
                        '--md', str(out_md)]) == 0
    data = json.loads(out_json.read_text())
    (summary,) = data['runs']
    assert data['unreadable'] == ['not-mounted']
    assert summary['hold']['code'] == 'attempt_schema_limit'
    assert summary['invalid_tool_calls'] == 1 and summary['corrections_charged'] == 1
    (session,) = summary['sessions']
    assert (session['phase'], session['role']) == ('discovery', 'author')
    assert session['max_parallel_tool_calls'] == 1
    assert session['corrections_charged'] == {'clean_dependency_install_only': 1}
    assert data['zero_hit_rules'] == ['MISS'] and data['unmeasurable_rules'] == ['NONE']
    assert {rule['anchor']: rule['hits'] for rule in data['rules']}['TOOLS'] == 1
    assert 'Rules with zero hits across 1 runs: MISS' in out_md.read_text()
