# Data Model: Documenter Workflow

## Constants

```python
KINDS = ("explanation", "how-to", "reference", "decision")
REQUIRED_HEADINGS = {
    "explanation": ("What it is", "How it fits", "Why it is this way", "Where to change it"),
    "how-to": ("Goal", "Prerequisites", "Steps", "Verify"),
    "reference": (),                     # at least one table or list whose entries each cite a path
    "decision": ("Context", "Decision", "Consequences", "Status"),
}
README_SECTIONS = ("Start here", "Architecture", "How to", "Reference", "Decisions", "Optional")
KIND_TO_SECTION = {"explanation": "Architecture", "how-to": "How to", "reference": "Reference", "decision": "Decisions"}
DOC_PATH_PREFIXES = ("docs/", "doc/")
DOC_ROOT_FILES = ("README", "CONTRIBUTING", "CHANGELOG")   # basename prefixes
POINTER_FILES = ("AGENTS.md", "CLAUDE.md")
POINTER_MARKERS = ("<!-- coordinare:wiki-pointer:start -->", "<!-- coordinare:wiki-pointer:end -->")
PLAN_CAP = 8
PAGE_MIN_CHARS = 400
PAGE_MAX_CHARS = 12000
SUMMARY_MAX_CHARS = 300
POINTER_MAX_LINES = 40
BAD_LINK_TEXT = ("here", "link", "this")
```

## WikiPage (inventory)

`path`, `kind: str | None` (None when frontmatter is missing), `title` (first H1 or the file stem), `citations: list[str]` (backticked tokens that look like repository paths and resolve, plus link targets that resolve to repository files), `links: list[str]` (relative `.md` targets under `docs/wiki/`), `size: int`.

## RepositoryLayout (init mode)

`project_name` (from `pyproject.toml`, `package.json` or the directory name), `packages: list[(path, size, has_tests)]` for top-level source directories, `has_ci`, `test_command_hint`.

## PagePlan

`path`, `kind`, `source: Literal["brief", "inventory", "init", "index"]`, `justification: str` (the brief topic, the changed files that hit the page's citations, or the layout item), `exists: bool`, `say: list[str]` (brief texts), `modules: list[str]`.

## PageEvidence

`commands: list[dict]` (command, exit_code, chars, refused, reason), `chars: int`.

## ModelPageWrite (the model-facing schema, per page)

`action: Literal["write", "unchanged", "retire"]`, `content: str (<= 12000)`, `reason: str (<= 300)`. `extra="forbid"`; no path field (the plan fixes it).

## PageResult

`path`, `kind`, `action`, `reason`, `citations_checked: int`, `citations_missing: list[str]`, `links_missing: list[str]`, `contract_failures: list[str]`, `size: int`, `dropped: bool`, `drop_reason: str | None`.

## DocsRecord

`mode`, `brief_present`, `changed_files: list[str]`, `diff_truncated`, `inventory_size`, `plan: list[PagePlan]`, `deferred: list[str]`, `refused_paths: list[str]`, `evidence: dict[path, PageEvidence]`, `results: list[PageResult]`, `files_written: list[str]`, `files_retired: list[str]`, `readme_generated: bool`, `pointers_refreshed: list[str]`, `commit_sha: str | None`, `verdict: Literal["docs_committed", "env_blocked"]`, `hold_reason: str | None`, `workflow_metrics: dict`.

## Rule predicates (the exact operands the gate tests mutate)

- `is_doc_path(path)`: starts with a `DOC_PATH_PREFIXES` entry, or its basename starts with a `DOC_ROOT_FILES` entry, or it is a `POINTER_FILES` entry. Mutation: drop `doc/`.
- `select_pages(brief, changed_files, inventory, cap)`: brief entries (deduplicated, doc paths only) then inventory pages whose citations hit a changed file (equality or directory prefix), README appended when any page selected, sliced to `cap`, overflow deferred. Mutations: skip the inventory pass; drop the README append; ignore the cap.
- `init_skeleton(layout, inventory, cap)`: README, architecture, setup, testing, then packages with tests largest first; existing pages keep their kind. Mutation: include packages without tests.
- `extract_citations(content)`: backticked tokens containing `/` or a file extension, plus link targets not ending in `.md` under the wiki. Mutation: drop link targets.
- `citations_exist(citations, tree)`: every citation is a file or directory in the tree. Mutation: always true.
- `links_resolve(links, pages_after_run)`: every wiki link names a page that exists after the run. Mutation: check the tree before the run instead.
- `contract_failures(kind, content)`: exactly one H1; frontmatter kind in KINDS; required headings present; headings unique with no link or inline code; fenced blocks with a language; no bad link text; size bounds; no changelog heading. Mutations: one per check.
- `is_changelog_heading(line)`: a heading containing `#\d+` or `changelog` (case folded). Mutation: only the word.
- `generate_readme(project, summary, pages)`: H1, blockquote (<= 300 chars), README_SECTIONS in order with each page listed exactly once under `KIND_TO_SECTION[kind]` (pages without a kind under `Optional`). Mutations: list a page twice; drop the blockquote.
- `readme_shape_ok(content, pages)`: one H1, blockquote directly under it, every page linked exactly once, only README_SECTIONS as H2. Mutation: allow a page linked twice.
- `render_pointer_section(entry, first_three)`: under 40 lines between the markers. Mutation: drop the line cap.
- `replace_between_markers(text, section)`: replaces the region, appends when missing, leaves the rest byte for byte. Mutation: replace the whole file.
- `accept_retire(plan_entry, inventory_page, tree)`: source inventory AND no citation exists. Mutation: allow brief-sourced retire.

## The canonical flow

1. intake: brief, diff (reviewer parser), mode, inventory, layout (init), tree listing (`git ls-files`).
2. plan: `select_pages` or `init_skeleton`; refused paths recorded; empty plan ends the run `docs_committed` with no files and no model call.
3. gather per page (code-built commands through the allow-list).
4. write per page (except README): one call, one reprompt.
5. gate per page: action rules, citations, links, contract; drops recorded.
6. README regenerated by code from the surviving inventory when any page changed.
7. pointers rendered and replaced between markers when the wiki changed.
8. write-free check (tree clean at start), then one `commit_files` call with writes and retirements; the init-mode PR open as today.
9. report `{"docs": DocsRecord, "workflow_metrics"}`; main.py maps `docs_committed` with `files_modified`, or `env_blocked`.

## Persona placeholders

| Kind | Placeholders |
| --- | --- |
| WRITE (per kind) | `{path}`, `{kind}`, `{required_headings}`, `{current_content}`, `{say}`, `{evidence}`, `{changed_hunks}`, `{writing_rules}` |

## Budgets (`DocumenterBudgets.from_env`)

| Env | Default |
| --- | --- |
| `DOC_PLAN_CAP` | 8 (never above 8) |
| `DOC_GATHER_MAX_COMMANDS` | 6 per page |
| `DOC_GATHER_MAX_OUTPUT_CHARS` | 4000 per command |
| `DOC_PAGE_MAX_CHARS` | 12000 |
