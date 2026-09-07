# Data Model: Closer Workflow

## ThreadComment

`author: str`, `body: str (<= 4000)`, `created_at: str`.

## Thread

`id: str`, `path: str`, `line: int >= 0`, `resolved: bool`, `outdated: bool`, `comments: list[ThreadComment]`.
Properties: `first_author` (comments[0].author or ""), `last_author` (comments[-1].author or ""), `raiser_only` (every author equals first_author).

## Classification

`thread_id`, `state: Literal["resolved", "stale", "answered", "open"]`, `rule: str` (the predicate that decided it).

## ModelJudgement (the model-facing schema)

`model_judgements_schema(max_threads)` builds `{"judgements": [{"thread_id": str, "addressed": bool, "quote": str (<= 300), "reason": str (<= 300)}]}` with `extra="forbid"` at both levels, at most `max_threads` entries, so a verdict key is a schema violation.

## Judgement

`thread_id`, `addressed: bool`, `quote: str`, `reason: str`, `accepted: bool`, `discard_reason: str | None`.

## ClosingRecord

`head_sha: str | None`, `threads_read: int`, `pages_read: int`, `classifications: list[Classification]`, `judgements: list[Judgement]`, `resolved: list[{thread_id, reason}]`, `open_threads: list[{thread_id, path, line, excerpt}]`, `verdict: Literal["approved", "changes_requested", "env_blocked"]`, `hold_reason: str | None`, `posted_review_url: str | None`, `workflow_metrics: dict`.

## Rule predicates (the exact operands the tests mutate)

- `is_resolved(t)`: `t.resolved`. Mutation: ignore the flag.
- `is_stale(t)`: `not t.resolved and t.outdated`. Mutations: drop the resolved clause; treat resolved-and-outdated as stale.
- `is_answered(t)`: `not t.resolved and not t.outdated and len(t.comments) >= 2 and t.last_author != t.first_author and t.comments[-1].created_at >= t.comments[0].created_at`. Mutations: drop the author comparison (the raiser's own follow-up would count); drop the outdated clause.
- `classify_thread(t)`: resolved, then stale, then answered, else open. Mutation: check answered before stale.
- `quote_found(quote, thread)`: the whitespace-folded quote is a substring of some comment body, and the quote is non-empty. Mutations: always true; accept an empty quote.
- `accept_judgement(j, sent_ids, threads)`: the thread was sent, and when `addressed` the quote is found. Mutations: accept an unsent thread id; skip the quote check.
- `verdict(open_threads)`: `changes_requested` when any remain, else `approved`. Mutation: approve with open threads.
- `to_resolve(classifications, judgements)`: the stale ids plus the accepted addressed ids. Mutations: include open ids; drop the stale ids.

## The canonical flow

1. intake: `fetch_review_threads` (paged), head SHA. A fetch failure ends the run `env_blocked`.
2. classify: every thread, by rule.
3. judge: one call if the answered set is non-empty, at most 20 threads, else skipped entirely (no model call).
4. gate: accept or discard each judgement; open threads are the `open` set plus the answered threads whose judgement was not accepted or was `not_addressed`.
5. verdict by code.
6. post one review naming resolved and remaining threads; a failure is a hold.
7. act: on approval only, resolve the stale and accepted-addressed ids; a resolution failure is a hold.
8. report `{"closing": ClosingRecord, "workflow_metrics"}`; main.py maps it.

## Persona placeholders

| Kind | Placeholders |
| --- | --- |
| JUDGE | `{threads}` (id, path, line and the full comment transcript per thread) |

## Budgets (`CloserBudgets.from_env`)

| Env | Default |
| --- | --- |
| `CLOSER_MAX_THREADS_PER_CALL` | 20 |
| `CLOSER_MAX_PAGES` | 5 |
