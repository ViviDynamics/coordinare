# Quickstart: verifying spec 153

## The claim

A board that knows nothing about GitHub can route card comments.

## Prove it

```bash
.venv/bin/pytest tests/unit/test_153_card_id_model.py -q
```

The load-bearing test drives the real `route_issue_comments` node with a provider whose cards
are keyed `PROJ-123`, and asserts the code-host service is never consulted for the read.

## Prove it costs nothing

```bash
.venv/bin/pytest tests/unit/test_153_card_id_model.py -q -k cost
```

Asserts the exact sequence of calls the code host receives is `["poll_board",
"get_issue_comments"]` — anything extra fails, including a call nobody predicted.

## Prove nothing else moved

```bash
git diff --stat main...HEAD -- tests/
```

Only `tests/unit/test_153_card_id_model.py` (new), `tests/unit/test_149_board_provider.py` (the
surface pin and the now-false gap assertions), and
`tests/unit/services/test_issue_comment_service.py` (the changed signature) may appear.

## Full gate

```bash
.venv/bin/ruff check src tests && .venv/bin/pytest tests/ -q
```
