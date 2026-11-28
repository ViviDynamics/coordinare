"""Load and check replay fixtures (format 1, written by scripts/replay_extract.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'replay'
FORMAT = 1
REPLY_KINDS = frozenset({'text', 'candidate', 'tool_calls'})
RECORDED_KEYS = frozenset({'refusal_codes', 'guidance_ids', 'charged'})


class FixtureFormatError(ValueError):
    """A fixture that does not follow format 1."""


@dataclass(frozen=True)
class ReplayStep:
    call: str
    session: str | None
    turn: int | None
    reply: dict[str, Any]
    recorded: dict[str, Any]
    request: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReplayFixture:
    run: str
    head: str
    hold: dict[str, Any]
    seam: str
    redaction: dict[str, Any]
    state: dict[str, Any]
    steps: tuple[ReplayStep, ...]
    synthetic: bool = False
    path: Path = field(default=FIXTURES)

    def step(self, call_suffix: str) -> ReplayStep:
        """The one step whose call id ends with ``call_suffix``."""
        matches = [step for step in self.steps if step.call.endswith(call_suffix)]
        if len(matches) != 1:
            message = f'{call_suffix!r} names {len(matches)} steps of {self.path.name}'
            raise KeyError(message)
        return matches[0]


def _require(condition: bool, path: Path, message: str) -> None:
    if not condition:
        raise FixtureFormatError(f'{path}: {message}')


def parse_fixture(data: Any, path: Path) -> ReplayFixture:
    _require(isinstance(data, dict), path, 'not a JSON object')
    _require(data.get('format') == FORMAT, path, f'format is {data.get("format")!r}, expected {FORMAT}')
    missing = {'run', 'head', 'hold', 'seam', 'redaction', 'state', 'steps'} - data.keys()
    _require(not missing, path, f'missing {sorted(missing)}')
    _require(isinstance(data['hold'], dict) and {'code', 'stage'} <= data['hold'].keys(), path, 'hold needs code, stage')
    _require(isinstance(data['redaction'], dict) and 'version' in data['redaction'], path, 'redaction needs a version')
    steps = []
    for index, step in enumerate(data['steps']):
        _require(isinstance(step, dict) and {'call', 'session', 'turn', 'reply', 'recorded'} <= step.keys(), path,
                 f'step {index} needs call, session, turn, reply, recorded')
        _require(bool(REPLY_KINDS & step['reply'].keys()), path, f'step {index} reply has none of {sorted(REPLY_KINDS)}')
        _require(step['recorded'].keys() >= RECORDED_KEYS, path, f'step {index} recorded needs {sorted(RECORDED_KEYS)}')
        steps.append(ReplayStep(step['call'], step['session'], step['turn'], step['reply'], step['recorded'],
                                step.get('request')))
    return ReplayFixture(data['run'], data['head'], data['hold'], data['seam'], data['redaction'], data['state'],
                         tuple(steps), bool(data.get('synthetic', False)), path)


def load_fixture(run: str, case: str, *, root: Path = FIXTURES) -> ReplayFixture:
    path = root / run / f'{case}.json'
    return parse_fixture(json.loads(path.read_text(encoding='utf-8')), path)


def all_fixture_paths(root: Path = FIXTURES) -> list[Path]:
    return sorted(path for path in root.glob('*/*.json'))
