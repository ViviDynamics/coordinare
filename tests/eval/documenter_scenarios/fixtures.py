"""Fixtures for the documenter workflow eval (spec 171 SC-006).

Each fixture builds a real temporary git repository (through the shared test
helper), carries the brief, the diff to produce, the canned model replies per
page path, and the expectations scoring checks. Live mode swaps the stub for
the gateway model and keeps the expectations loose where a real model varies.
"""
from __future__ import annotations

from dataclasses import dataclass

PAYMENTS_PAGE = """---
kind: reference
---
# Payments module

The payments module lives in `src/payments.py` and stores charges through `src/db.py`.

| Name | What it is |
| --- | --- |
| `src/payments.py` | `charge(conn, amount)` inserts a charge row and returns True. |
| `src/db.py` | The sqlite connection factory the module uses. |

See [Architecture](architecture.md) for how requests reach it.
""" + ("\nCharges are integers in the smallest currency unit; the route in `src/app.py` returns a plain confirmation string.\n" * 3)

BRIEF = {"summary": "Add the payments module", "docs": [{"topic": "Payments module", "location": "docs/wiki/payments.md", "say": "Describe charge() and where charges are stored.", "kind": "reference"}], "modules": ["src/payments.py"]}


@dataclass(frozen=True)
class Expectation:
    verdict: str
    min_written: int
    max_written: int
    model_calls: int | None          # exact in stubbed mode; None when a real model may vary
    committed: bool
    dropped_paths: tuple[str, ...] = ()
    readme_generated: bool = False
    pointers: bool = False
    live_verdicts: tuple[str, ...] = ("docs_committed",)


@dataclass(frozen=True)
class Fixture:
    name: str
    change: str                       # "payments", "trivial", "init"
    brief: dict
    replies: dict[str, dict]          # page path -> model reply
    expect: Expectation
    with_wiki: bool = True
    agents_md: str | None = None
    mode: str = "update"


def _reply(action, content="", reason=""):
    return {"action": action, "content": content, "reason": reason}


# 367: model_calls counts the reading too. Only the fixtures that regenerate the
# index need it -- the index carries the project's name, and that name used to
# come free from a pyproject.toml probe that worked for Python and Node and
# nothing else. Fixtures whose pages are all dropped never reach that point and
# still make two calls, and `trivial` still makes zero, which is the laziness
# this change is careful about.
TRIVIAL = Fixture(name="trivial", change="trivial", brief={}, replies={},
                  expect=Expectation(verdict="docs_committed", min_written=0, max_written=0, model_calls=0, committed=False))
FEATURE = Fixture(name="feature", change="payments", brief=BRIEF, replies={"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE)},
                  expect=Expectation(verdict="docs_committed", min_written=3, max_written=6, model_calls=3, committed=True, readme_generated=True, pointers=True))
SHAPE = Fixture(name="shape", change="payments", brief=BRIEF,
                replies={"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE + "\n## Card #7 changes\n- added charge\n")},
                expect=Expectation(verdict="docs_committed", min_written=0, max_written=6, model_calls=2, committed=False, dropped_paths=("docs/wiki/payments.md",), live_verdicts=("docs_committed",)))
HALLUCINATED = Fixture(name="hallucinated_citation", change="payments", brief=BRIEF,
                       replies={"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE.replace("`src/payments.py` and stores", "`src/payments/ledger.py` and stores"))},
                       expect=Expectation(verdict="docs_committed", min_written=0, max_written=6, model_calls=2, committed=False, dropped_paths=("docs/wiki/payments.md",)))
INIT = Fixture(name="init", change="init", brief={}, replies={}, with_wiki=False, mode="init",
               expect=Expectation(verdict="docs_committed", min_written=4, max_written=11, model_calls=None, committed=True, readme_generated=True, pointers=True))
POINTERS = Fixture(name="pointers", change="payments", brief=BRIEF, replies={"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE)},
                   agents_md="# Agents\n\nBuild with `make`.\n\n<!-- coordinare:wiki-pointer:start -->\nold\n<!-- coordinare:wiki-pointer:end -->\n\nMore rules.\n",
                   expect=Expectation(verdict="docs_committed", min_written=3, max_written=6, model_calls=3, committed=True, readme_generated=True, pointers=True))

FIXTURES: list[Fixture] = [TRIVIAL, FEATURE, SHAPE, HALLUCINATED, INIT, POINTERS]
