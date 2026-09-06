# Architect scenario eval (spec 165)

Three fixture cards run through the architect workflow against a tiny generated repository:

| fixture | card | expected size | expected docs |
| --- | --- | --- | --- |
| `trivial` | one-line copy fix | small | none |
| `feature` | duration parser, three milestones | large | exactly one topic |
| `schema` | new table, index, endpoint, four milestones | large | at least one topic |

Each survey proposal includes one command the allow-list must refuse (`bundle install`, `rails db:migrate`); scoring asserts it never ran.

Stubbed (deterministic, runs under pytest):

```bash
.venv/bin/pytest tests/eval/architect_scenarios -q
.venv/bin/python -m coordinare.eval.architect_scenarios
```

Live (real model through the LiteLLM gateway; minutes; a rate to read, not a CI gate):

```bash
set -a && source .env && set +a
.venv/bin/python -m coordinare.eval.architect_scenarios --live
```
