"""Fixtures for the documenter workflow eval (spec 171 SC-006).

Each fixture builds a real temporary git repository (through the shared test
helper), carries the brief, the diff to produce, the canned model replies per
page path, and the expectations scoring checks. Live mode swaps the stub for
the gateway model and keeps the expectations loose where a real model varies.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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
    change: str                       # "payments", "trivial", "init", "mkdocs", "sphinx", "monorepo"
    brief: dict
    replies: dict[str, dict]          # page path -> model reply
    expect: Expectation
    with_wiki: bool = True
    agents_md: str | None = None
    mode: str = "update"
    workflow_env: dict[str, str] = field(default_factory=dict)  # 415: e.g. DOCS_CREATE_POINTERS, DOCS_ROOT


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
                  expect=Expectation(verdict="docs_committed", min_written=3, max_written=6, model_calls=3, committed=True, readme_generated=True, pointers=True),
                  workflow_env={"DOCS_CREATE_POINTERS": "1"})  # 415: absent pointer files are created only on opt-in
SHAPE = Fixture(name="shape", change="payments", brief=BRIEF,
                replies={"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE + "\n## Card #7 changes\n- added charge\n")},
                expect=Expectation(verdict="docs_committed", min_written=0, max_written=6, model_calls=2, committed=False, dropped_paths=("docs/wiki/payments.md",), live_verdicts=("docs_committed",)))
HALLUCINATED = Fixture(name="hallucinated_citation", change="payments", brief=BRIEF,
                       replies={"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE.replace("`src/payments.py` and stores", "`src/payments/ledger.py` and stores"))},
                       expect=Expectation(verdict="docs_committed", min_written=0, max_written=6, model_calls=2, committed=False, dropped_paths=("docs/wiki/payments.md",)))
INIT = Fixture(name="init", change="init", brief={}, replies={}, with_wiki=False, mode="init",
               expect=Expectation(verdict="docs_committed", min_written=4, max_written=11, model_calls=None, committed=True, readme_generated=True, pointers=True),
               workflow_env={"DOCS_CREATE_POINTERS": "1"})  # production init dispatch (daemon.py) opts in
POINTERS = Fixture(name="pointers", change="payments", brief=BRIEF, replies={"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE)},
                   agents_md="# Agents\n\nBuild with `make`.\n\n<!-- coordinare:wiki-pointer:start -->\nold\n<!-- coordinare:wiki-pointer:end -->\n\nMore rules.\n",
                   expect=Expectation(verdict="docs_committed", min_written=3, max_written=6, model_calls=3, committed=True, readme_generated=True, pointers=True))

# 415: the docs root is discovered from the repository, not assumed to be
# docs/wiki. One fixture per shape the discovery handles: an MkDocs site, a
# Sphinx source tree, a monorepo whose packages split, and a repository with
# no documentation at all.
DOCS_PAGE_BODY = """
| Name | What it is |
| --- | --- |
| `src/payments.py` | `charge(conn, amount)` inserts a charge row and returns True. |
| `src/db.py` | The sqlite connection factory the module uses. |

""" + ("\nThe route in `src/app.py` returns a plain confirmation string.\n" * 3)

MKDOCS_PAGE = f"""---
kind: reference
---
# Payments integration

Charges flow through `src/payments.py` and are stored via `src/db.py`.
{DOCS_PAGE_BODY}"""
MKDOCS_BRIEF = {"summary": "Add the payments module", "docs": [{"topic": "Usage", "location": "docs/usage.md", "say": "Describe how payments integrate with the app.", "kind": "reference"}], "modules": ["src/payments.py"]}
MKDOCS = Fixture(name="mkdocs", change="mkdocs", brief=MKDOCS_BRIEF, replies={"docs/usage.md": _reply("write", MKDOCS_PAGE)},
                 with_wiki=False,
                 expect=Expectation(verdict="docs_committed", min_written=2, max_written=2, model_calls=2, committed=True, readme_generated=True))

SPHINX_PAGE = f"""---
kind: reference
---
# Payments

Charges flow through `src/payments.py` and are stored via `src/db.py`.
{DOCS_PAGE_BODY}See [Usage](usage.md) for day-to-day operation.
"""
SPHINX_BRIEF = {"summary": "Add the payments module", "docs": [{"topic": "Payments", "location": "doc/source/payments.md", "say": "Describe charge() and where charges are stored.", "kind": "reference"}], "modules": ["src/payments.py"]}
SPHINX = Fixture(name="sphinx", change="sphinx", brief=SPHINX_BRIEF, replies={"doc/source/payments.md": _reply("write", SPHINX_PAGE)},
                 with_wiki=False,
                 expect=Expectation(verdict="docs_committed", min_written=2, max_written=2, model_calls=2, committed=True, readme_generated=True))

MONO_PAGE = """---
kind: reference
---
# Services

| Name | What it is |
| --- | --- |
| `apps/web/main.py` | The web entrypoint. |
| `apps/api/api.py` | The API entrypoint. |

Both services declare their dependencies in `package.json`.

""" + ("\nThe web entrypoint in `apps/web/main.py` never imports the api in `apps/api/api.py`, and each side keeps its own `package.json`.\n" * 3)
MONO_BRIEF = {"summary": "Split the monorepo services", "docs": [{"topic": "Services", "location": "docs/wiki/services.md", "say": "Describe the two services and where their dependencies live.", "kind": "reference"}], "modules": ["apps/web/main.py"]}
MONOREPO = Fixture(name="monorepo", change="monorepo", brief=MONO_BRIEF, replies={"docs/wiki/services.md": _reply("write", MONO_PAGE)},
                   expect=Expectation(verdict="docs_committed", min_written=2, max_written=2, model_calls=2, committed=True, readme_generated=True))

NODOCS_PAGE = PAYMENTS_PAGE.replace("See [Architecture](architecture.md) for how requests reach it.\n", "")
NODOCS = Fixture(name="no_docs", change="payments", brief=BRIEF, replies={"docs/wiki/payments.md": _reply("write", NODOCS_PAGE)},
                 with_wiki=False,
                 expect=Expectation(verdict="docs_committed", min_written=2, max_written=2, model_calls=2, committed=True, readme_generated=True))

FIXTURES: list[Fixture] = [TRIVIAL, FEATURE, SHAPE, HALLUCINATED, INIT, POINTERS, MKDOCS, SPHINX, MONOREPO, NODOCS]
