# Feature Specification: the remaining config writes take a version

**Feature Branch**: `158-guard-remaining-config-writes`
**Created**: 2026-08-31
**Issue**: #241

## Context

Optimistic concurrency arrived piecemeal. Spec 081 guarded the catalog and routing writes; spec
156 gave `PUT /api/config/global` a guard and spec 157 made it required. Five routes that write
the same `config.yaml` still take no version at all:

| route | writes via |
|---|---|
| `POST /api/symphonies` | `_persist_symphony_configs` |
| `PUT /api/symphonies/{name}` | `_persist_symphony_configs` |
| `DELETE /api/symphonies/{name}` | `_persist_symphony_configs` |
| `PUT /api/personas/{role}` | `save_persona` / `update_persona` |
| `DELETE /api/personas/{role}` | `reset_persona` |

`persona_service.py:770` already admits it: the write is atomic via `os.replace`, "but two
overlapping calls can still lose each other's" changes. Atomic means the file is never
half-written, not that your edit survives.

This matters more now than it did last week, because one endpoint refuses a versionless write
with 428 while its neighbours accept one silently. A half-guarded surface is worse than an
unguarded one: an operator who meets the strict endpoint reasonably concludes the surface is
strict.

## How the version travels, and why differently here

The guarded endpoints take the version in the request body (`expected_hash`, `base_hash`). Two of
these five are `DELETE`, which conventionally has no body — and inventing one for them would
leave the surface inconsistent in a second way while fixing the first.

So these take **`If-Match`**, the header HTTP already has for exactly this, paired with the
`ETag` spec 156 added. `PUT /api/config/global` gains `If-Match` as an accepted alternative to
its body field, so there is one mechanism to document going forward and nothing existing breaks.

## Requirements

- **FR-001**: Each of the five routes MUST require a version and refuse a request without one.
- **FR-002**: The refusal MUST be **428**, naming `If-Match` and where to get the value.
- **FR-003**: A stale version MUST be refused with **409**, leaving the file untouched.
- **FR-004**: A refused request MUST NOT modify `config.yaml` — nor any other state, so a
  refused symphony delete must not remove it from memory either.
- **FR-005**: The `GET` each page loads from MUST carry the current version as an `ETag`,
  for the routes guarded by the header vehicle. The 078 routing table and the 081 catalogs
  take their version in the body and hand it back the same way (`content_hash` out,
  `base_hash` in), which is a whole vehicle rather than half of one; a seventh review round
  read this requirement as covering them and reported `GET /api/config/routing` as missing
  an `ETag` it was never meant to send.
- **FR-006**: Each page MUST send the version it loaded with, and fail closed without one.
- **FR-007**: `PUT /api/config/global` MUST accept `If-Match` as an alternative to
  `expected_hash`, with the body field winning if both are sent.
- **FR-008**: One implementation of "require and check a version", not five.

## Success Criteria

- **SC-001**: No route can write `config.yaml` without a version. Asserted by enumerating route
  handlers, so a route added later is caught rather than assumed.
- **SC-002**: A refused write leaves the file byte-identical.
- **SC-003**: A refused symphony delete leaves the symphony present in memory as well as on disk.
- **SC-004**: Existing guarded endpoints keep their current behaviour.

## The breaking change

The same shape as spec 157's, and the same reasoning: these are undocumented internal endpoints
behind the dashboard's origin guard, the repo is pre-launch, and silently losing an operator's
edit is worse than a loud failure. Automation calling them begins failing with 428 until it sends
`If-Match`.

## How the five were found

Worth recording, because two earlier attempts looked conclusive and were wrong. A backwards walk
from a line number to the nearest route decorator named the wrong endpoint entirely (the write
lived in a helper defined below it). A grep for that helper's callers was right but incomplete —
it could not see the persona routes, which reach a different writer. Only walking the AST for
every route handler that reaches any known writer produced the full set, and that is what SC-001
asserts, so the next route to be added is caught by a test rather than by someone remembering.

## The guard was only half the change

The first implementation of this spec guarded all five routes, passed 5,394 tests, and broke
every one of the five actions in the dashboard UI.

Nothing in the five handlers was wrong. The problem was on the other side of them: none of the
seven front-end call sites sent an `If-Match`, and no GET the pages load from returned an ETag,
so there was no version for them to send. The 428's own message — "the matching GET returns the
current version as an ETag" — was false for all five routes at the moment it was written. The
suite was green because the tests threaded the header in themselves, which is precisely the shape
of a test that confirms the code does what it does.

So the requirements below are part of the same change, not a follow-up:

- **FR-009**: Every GET a config-editing page loads from returns the current version as an ETag,
  quoted per RFC 7232 §2.3. A guard demanding a version the client cannot obtain is not a guard.
- **FR-010**: Every guarded write returns the *post-write* version, so a second save in the same
  session succeeds without reloading the page. This project has now shipped the missing-refresh
  bug twice (156 left `_adminCfgHash = null` after a save; 157's Apply button read whatever hash
  another page had left behind), which is reason enough to treat it as a requirement.
- **FR-011**: A 409 tells the operator what to do about it. "Config changed since this page
  loaded. Reload and re-apply." is actionable; echoing a status code is not.

- **SC-005**: Every version-guarded route has a front-end caller that sends a version, asserted by
  cross-referencing the route enumeration against the inlined JS rather than by reading either
  side alone. Both halves were individually correct; only comparing them finds this.
- **SC-006**: A version handed back by a write is accepted by the next write, and a stale one is
  still refused — so SC-005 cannot be satisfied by dropping the check.

Both enumerations were then mutation-tested rather than trusted: removing any one of the five
server guards, or any one of the client's version expressions, makes the suite fail. A test that
asserts an inventory is worth only what it catches when the inventory is wrong.

## What this says about the review that found it

The defect was not subtle and the tests did not find it, because the tests and the code were
written from the same belief about who sends what. What found it was asking a different question —
"does the thing that calls this still work?" — against the actual caller. That question is cheap
and belongs in the loop for any change to a request contract.

## What the third review round found

Four rounds of implementation and two review rounds had passed before these surfaced,
so they are recorded rather than quietly fixed.

**The `delete_symphony` bug was in two more handlers, and I had said it was not.**
`create_symphony` and `update_symphony` also write `daemon.state` and bump
`config_version` before they persist, so a 428 or 409 left the symphony added or
updated in memory with the version bumped: the dashboard showed a change that was
never written, and the next reload silently undid it. The commit that fixed
`delete_symphony` asserted in a code comment that "the other four sit beside the
write". That was false when written.

It survived because of how it was tested. The parametrized refusal tests compare
file bytes, and the only in-memory assertion was written for `delete_symphony` --
the handler where the bug had already been found. Checking one handler for a defect
found in that handler is how the other two stayed broken.

- **FR-013**: A refused write leaves in-memory state untouched, asserted for all
  five routes, comparing values as well as key sets. `configs[name] = updated` keeps
  the key set identical, so a comparison of names alone reports the PUT case as clean
  -- a throwaway probe did exactly that and passed the broken handler.
- **SC-007**: The ordering is also asserted structurally: no handler may assign to
  `daemon.state` before its version guard. Reading the file is what found this, and a
  test that only sends requests would pass again the moment a handler is reordered in
  a way the five cases happen not to cover.

**Four of seven client error paths reported a version conflict as a number.** The
helper went in and was wired into three call sites. The rest showed `Error 409`,
`Error resetting`, and `Error: Conflict`. One called the helper with a null body, so
the `conflict` flag it keys on could not be seen -- the call was there and the
argument was not, which is why it read as correct.

- **FR-014**: Every version-guarded write reports a conflict actionably, asserted by
  scanning each call site's response handling. Three handlers qualify, because the
  081 config page got there first with `cfgHandleSave`/`cfgConflict` and 156's global
  saves branch on the status inline. Naming all three beats naming one and calling
  the others broken.

**Both version guards violated a documented contract.** `guard_concurrency` lets
`OSError` propagate on purpose: `compute_content_hash` returns the empty-file
sentinel for a present-but-unreadable file, so a bare comparison would let that
sentinel match and clobber data the UI never read. Its docstring says callers should
"degrade to a structured forbidden result rather than proceeding". Catching only
`ConcurrencyConflictError` turned that into a 500 with a traceback, in this spec's
helper and in 157's endpoint.

- **FR-015**: A present-but-unreadable config file is a 403 with the same message
  `_conflict_result` has returned for the 081 writes since spec 081 -- not a 500.
  Asserted by making the file unreadable and sending the request, and structurally,
  so a third guard cannot forget.

Every one of these was reintroduced on a copy afterwards to confirm the new test
fails. Two of the tests already in the file did not, before this round.

**And one note on the review itself.** The only finding the verify stage refuted was
true. Three skeptics killed it on location grounds -- right about the line numbers,
wrong about the defect -- and it was fixed only because it was a sibling of three
others. "Is the address right" is the wrong question to refute on; "is the defect
real" is the one that matters.

## What the fourth review round found

Three findings, all real, none refuted — and two of them were defects in the fix the
third round had just produced.

**The 403 named the config file's absolute path.** `str(OSError)` is
`[Errno 13] Permission denied: '/etc/coordinare/config.yaml'`, and that body reaches
the browser. The message had been copied from `_conflict_result` *deliberately*, for
consistency with the 081 writes, and the leak came with it. The original had it since
spec 081.

- **FR-016**: No response body carries a caught `OSError` or `yaml.YAMLError` whole.
  `safe_failure_reason()` returns `strerror` for an `OSError` and a description of the
  failure for anything else. One implementation, used at all six sites.
- **SC-008**: Asserted structurally, so the next handler cannot reintroduce it. That
  test is what turned two known leaks into six found ones — two of them in the persona
  write handlers this spec guards.

The first attempt at that fix was wrong, and three existing tests said so.
`exc.strerror` exists only on `OSError`; one of those handlers also catches
`UnicodeDecodeError` and `yaml.YAMLError`, so the leak became an `AttributeError` — a
500 where a structured result had been. The tests that caught it are named
`..._is_forbidden_not_500`, which is exactly what they are for.

Fixing it properly then surfaced a **second form of the same leak** that checking for
`OSError` could never have found: a `yaml.YAMLError`'s own message carries the path, as
`in "/etc/coordinare/config.yaml", line 3, column 1`. So `{exc}` there leaked for two
unrelated reasons, and the structural test now covers both.

**The structural ordering test understood one form of mutation.**
`test_the_guard_sits_above_every_mutation` inspected only `daemon.state[...] = ...`, so
`daemon.state.update({...})`, `.pop()`, `setdefault`, `del`, and `+=` above a guard
would all have passed. All five now fail it, each verified by inserting it.

- **FR-017**: `update_symphony` and `delete_symphony` copy the symphony dict, as
  `create_symphony` already did. They held the live `daemon.state` object, so the guard
  sitting above every mutation was the only thing between a refused write and corrupted
  shared state, with nothing to roll back from. Nothing was broken; this makes a future
  ordering mistake a bug rather than a catastrophe.

## What four rounds of this suggest

Recorded because the shape is more useful than the individual defects.

Every round found something, and almost all of it was in code the previous round had
just produced. The guard itself — the thing the issue asked for — was right in the
first commit and never changed. Everything since has been the surrounding contract:
who sends the version, where they get it, what a refusal leaves behind, what the error
says, what the error says *too much of*.

Three defects were invisible to tests written alongside the code, and visible
immediately to a test written against the *caller*: the client sent no version; a
refusal mutated memory; the 403 leaked a path. In each case the code and its tests
shared a belief, and nothing in the module could contradict it.

Two defects were found only by mutation-testing a test — reintroducing the fault to see
whether the test fails. Two tests already in this file did not, and both were ones this
spec had described as load-bearing.

And one process note: the only finding the third round's verify stage refuted was true.
Three skeptics killed it on location grounds, right about the line numbers and wrong
about the defect. The fourth round's prompt said to refute the defect and never the
address; it raised three and refuted none, and all three were real.

## What the fifth review round found

Three confirmed, and all three were one defect: a **third vector** for the path leak.

Rounds three and four chased `OSError`, then `yaml.YAMLError`. The persona handlers
return every `ValueError` from `save_persona` verbatim as a 400 body, and
`save_persona` raised `ValueError(f"Config file does not exist: {config_path}")`.

The structural test built in round four to catch exactly this class could not see it,
twice over: it inspected only handlers catching `OSError`, and only f-string
interpolation, while this was `str(exc)` on a `ValueError`. A test written against the
instance in front of it rather than the class it was named for.

- **FR-018**: `save_persona` names no path. Every caller already knows it, and the
  handler's 400 body reaches the browser. Reachable only by racing the handler's own
  `is_file()` check, which is narrow — and the message never needed the path.
- **FR-019**: Both persona `ValueError` branches log. The body is deliberately terse
  now, so an operator debugging a 400 otherwise has nothing.
- **SC-009**: The leak test covers five forms — `{exc}`, `{exc!s}`, `str(exc)`,
  `.format(exc)`, `"..." % exc` — each verified by mutation, with `str(exc)` inside a
  log call exempt, because the log is exactly where the path belongs.

**Two findings were refuted as "technically accurate, however".** One said the
structural test missed `str(exc)` and friends. That was plainly correct, and it is the
very form the confirmed defect used — the verify stage argued the narrowness was
harmless while a live instance of it sat in the same file. The other said the
chmod-based coverage of `safe_failure_reason` vanishes when tests run as root; closing
that was cheaper than deciding who CI runs as, so `TestSafeFailureReasonWithoutChmod`
now covers it unconditionally, including a single-argument `OSError` (where `strerror`
is `None`) and a non-`str` `strerror`.

That is the second round in which a true finding was refuted on a technicality. The
first was refuted on line numbers; this one on "but nothing reaches it yet". Both
refutations were accurate about what they addressed and wrong about what mattered.

## What five rounds add up to

The guard the issue asked for was correct in the first commit and never changed. Ten
commits of work since have all been the contract around it: who sends the version,
where they obtain it, what a refusal leaves behind, what the error says, and what the
error says too much of.

The defects clustered into three kinds.

**Beliefs shared by code and its tests.** The client sent no version; a refusal mutated
memory; a 403 named the config file. In each case the test and the code were written
from the same assumption, so nothing in the module could contradict it. What found them
was a question asked from outside: does the caller still work, what else does this
handler write, what does this body contain.

**Tests that asserted an inventory without being able to detect it changing.** Four
were fixed only because they were mutation-tested. Two of those were tests this spec had
already described as load-bearing.

**A test encoding an instance instead of a class.** The leak test was written for
`OSError` in an f-string, and then missed `yaml.YAMLError` and `str(exc)` — the same
defect, twice, in the same file it was guarding.

## What the sixth review round found

Nineteen findings raised across five lenses, sixteen survived the verify stage, and
six survived being checked by hand. The gap between those last two numbers is itself
the round's most useful result.

**The four concurrency findings were wrong, and unanimously confirmed.** Three
skeptics each said the same thing: the guard is checked at the top of the handler and
the file is written further down, so two requests can interleave between them and lose
each other's edits. It reads as obviously true. It is not, and one question settles
it: where are the suspension points?

| handler | guard | writes | `await` |
| --- | --- | --- | --- |
| `create_symphony` | 4729 | 4738, 4761 | 4668 |
| `update_symphony` | 4895 | 4905, 4931 | 4857 |
| `delete_symphony` | 4979 | 4995, 5011 | none |
| `update_persona` | 5902 | 5906, 5932 | 5879 |
| `reset_persona_endpoint` | 5972 | 5976, 5990 | none |

Every `await` is above the guard, reading the request body. All five are `async def`
on one event loop, so between the guard and the write there is no point at which
another request can run. The interleaving the finders described has nowhere to happen.
A separate OS process editing the file leaves a window of microseconds, which is
unchanged from the 081 writes and is not something this branch introduced.

Worth recording because it is the opposite failure to the one the last two rounds
produced. Round four refuted a true finding on line numbers; round five refuted one on
"nothing reaches it yet". This round confirmed a false one three times over, from three
different angles, because all three angles shared the reviewers' assumption that a
handler which looks sequential can be interrupted anywhere. Agreement between
skeptics is not evidence when they reason from the same premise.

### The six that were real

**FR-020**: the persona writes catch what their writer actually raises.
`save_persona` and `reset_persona` both begin with
`yaml.safe_load(config_path.read_text())`, so a config file that is not valid YAML or
not valid UTF-8 raises out of them. The handlers caught `ValueError` and `OSError`,
and neither is a superclass of `yaml.YAMLError`, so that was a 500 with a traceback.
`UnicodeDecodeError` *is* a `ValueError`, so it was being answered as a 400: a file
corrupt on disk reported as a bad request. The symphony write does not have this hole,
because it wraps its persist in `except Exception`. The same broken file was a
structured refusal on one route and an unhandled crash on another.

**FR-021**: `If-Match` is parsed as a header. RFC 7232 3.2 allows a comma-separated
list, optional whitespace, and weak tags; the parse was
`removeprefix("W/").strip('"')`, correct for the one shape this dashboard's own JS
sends and wrong for every other legal one. The harm was not that a list failed. It was
that it failed as a 409 saying the config had changed, which is untrue, and this spec
has already shipped one error message that lied about how to satisfy it. The wildcard
`*` is refused rather than honoured, because "any current representation" is the guard
opted out of, and it is refused *as a wildcard* so the message says so. Splitting the
header into tags then introduced a new hole in the same commit: an empty list (`,,`)
iterated zero times and every tag matched vacuously, so a header naming no version
would have passed the guard. It gets the 428 a missing header gets.

**SC-010, and four tests that could not detect their own subject changing.** All four
were fixed only because they were mutation-tested, and all four are shapes this file
has now found before:

- The ordering test read `daemon.state[...] = ...` and the mutator methods round four
  added, but not `daemon.state = {...}` (the target is an `Attribute`, not a
  `Subscript`) or `daemon.state["a"]["b"] = ...`. It also skipped any handler not
  named in a literal set of three, so a sixth symphony handler was invisible. The set
  is derived from the route enumeration now.
- The route enumeration matched writer names as text in the handler's own body, so a
  handler delegating to a local helper that writes reached the file while appearing to
  touch nothing. It is closed over the call graph now, by fixpoint.
- The client cross-reference extracted URLs with `r"'([^']*)'"` and the verb with
  `method:\s*'...'`. Every call site in this file happens to be single-quoted, so it
  worked. A refactor to double quotes or a template literal would have produced no
  literals at all, dropping that route from the cross-reference silently rather than
  failing it, with the version gone from the call.
- The leak detector, twice widened, still demanded an `ast.Name`, so `{exc.args[0]}`
  was invisible, and so was `repr(exc)`. It is inverted now: any expression that
  reaches the caught exception leaks, except the two that are named safe
  (`exc.strerror` and `safe_failure_reason(exc)`). A seventh form of the same mistake
  has nowhere to hide, and adding a genuinely safe accessor is a deliberate act rather
  than an omission.

Each fix was verified by inserting the fault and watching the test fail: three
mutations for the ordering test, one for the enumeration (confirmed invisible to the
previous logic, not merely caught by the new one), three for the cross-reference, and
six for the leak detector, of which the sixth is `exc.strerror` and is expected to
pass.

### The pattern, after six

Five rounds of defects clustered into three kinds; this round adds a fourth, and it is
about the review rather than the code.

The three test findings and the enumeration finding are all the same shape as rounds
four and five: a test that asserts an inventory while being unable to notice the
inventory changing. Four rounds have now found one, each time in a test the previous
round had written or widened. The lesson is not that the tests were careless. It is
that a test written from the same example the code was written from inherits the
example's boundaries, and only mutation reveals where they are.

The new one is that a confirmed finding is not a true finding. The concurrency lens
produced four, all internally consistent, all verified by three independent skeptics,
and all false, because none of them asked what a coroutine actually does between two
statements. What settled it was not more review. It was reading the ASTs and listing
the `await`s, and that took less time than the reviewing did.

## What the seventh review round found

Built to answer round six's failure rather than to repeat it. Round six produced four
findings that three skeptics each confirmed and that were all false, because none of
the seven agents involved ever tried to make the failure happen. So this round admitted
a finding only if the reporter ran something that demonstrated it, and kept it only if
a *skeptic independently reproduced it*. Ten findings, three survived, two were real.

**FR-022**: the tag scanner respects quotes. `_entity_tags`, which round six wrote,
split on every comma and then removed quotes. That is the obvious order and the wrong
one: RFC 7232 2.3 makes `etagc` `%x21 / %x23-7E`, so a comma is legal inside a tag,
and `"abc,def"` was being torn into two. The same parse also left a stray quote welded
to the hash in `W/ "x"`, so a malformed version was mangled into a different string
rather than simply failing to match. No version this deployment issues contains a
comma, and both failures refused rather than permitted, which is why this is recorded
as a parser defect and not as an exposure. It is fixed because a parser that answers
the wrong question about a legal input is wrong regardless of who is currently asking,
and because this file has now twice recorded "nothing reaches it yet" being wrong.

**FR-023**: the derivations notice the source changing. `_reaching_a_writer` and
`_guarded_write_handlers`, both added in round six, were `@lru_cache(maxsize=1)` over
no arguments: a cache that cannot notice its input changing, guarding tests whose
entire purpose is to notice their input changing. The verify stage dropped this one by
majority, and it was kept anyway, because of how this spec is checked. Every structural
fix since round four was verified by editing `dashboard.py` and re-running, and a
derivation answering from before the edit makes a mutation look caught when it was not.
They are keyed on the source now, and still cached.

**The third survivor was not a defect.** `GET /api/config/routing` returns its version
in the body rather than as an `ETag`, which FR-005 appeared to forbid. The 078 routing
table takes its version in the body and hands it back the same way, end to end, and its
writes are guarded; it is a whole vehicle, not half of one. FR-005 has been narrowed to
say so, since the ambiguity was in the requirement rather than in the reader.

### What changed in how the round was run

Two rules, both paid for by round six:

- A finding must carry the command that was run and its real output. Reasoning in the
  evidence field is not evidence.
- A finding survives only if a skeptic *reproduced* it, not merely agreed with it.

All four of round six's false concurrency findings would have died on the second rule.
None of their reporters ever ran anything; they read a handler top to bottom, saw a
gap between the guard and the write, and described what could happen in it. Three
skeptics then agreed, from the same premise, without checking either.

The cost is visible in the shape of this round: ten findings against round six's
nineteen, and the two that survived are narrower than anything in rounds three through
five. That is what running out of defects looks like from the inside, and it is the
first round where the surviving findings are both in code the *previous round wrote*
rather than in the original change.

## What the eighth review round found

Nothing. Four lenses, none of which raised a single finding, and the first round of the
eight to come back empty.

An empty result is the one result a review cannot be trusted to report honestly, so it
was checked rather than accepted. The four agents made 113 tool calls between them,
including copying the repository to a scratch directory to mutate it safely, so they
did the work and found no defect. Then the two properties that actually matter were
verified by hand rather than taken on the round's word.

**The scanner against the grammar.** A second parser, written from RFC 7232 2.3 and
not from `_entity_tags`, over a generated corpus. On grammar-legal headers the two
agree everywhere: 0 disagreements in 4,000 generated lists, and 0 round-trip failures
across 2,000 more. They differ on 2,548 malformed inputs, all of one family: an
unterminated quote, or a quote inside an unquoted token, where the two implementations
recover differently from something no conforming client can send. Both recoveries
produce a tag that matches nothing, so both refuse the write.

That last point is the one that needed proving rather than arguing, so it is now a
property test rather than a paragraph: **no header that does not contain the current
version can parse to it.** 1,517 inputs, including 1,500 generated from the characters
that appear in a version header. None conjured a version. At the HTTP level, 29 hostile
headers were driven through the persona write against a real config file: every one was
refused, the file was byte-identical throughout, and every legal shape of the true
version was accepted.

**The mutations, re-run.** Round seven changed the caching underneath two of the
structural tests, which is exactly how a test gets disarmed without anyone noticing.
All eight mutations the earlier rounds recorded as verified still fail at HEAD:
`daemon.state = {...}` and a nested subscript above the guard, a writer reached through
a local helper, four forms of exception leak, and a double-quoted fetch with the version
removed.

### SC-011, and what the round left behind

Every earlier test of the parser is an example someone thought of, which is the failure
this file has now recorded four times in its own tests. Three property tests were added
in place of the finding the round did not produce: the two parsers agree on legal input,
a legal list round-trips to exactly the tags it named, and no header can conjure a
version it does not contain. Seeded, so a failure is reproducible and CI cannot flake.

### Eight rounds, in one paragraph

The guard the issue asked for was correct in the first commit and never changed. Every
defect since was in the contract around it, and after round three, every defect was in
the previous round's own work: the fix for the path leak leaked twice more, the test
built to catch that class could not see the class, the parser written to read a header
correctly read it wrongly, and the cache added to make the derivations fast made them
unable to notice the file changing. That regress ends here, at a round that found
nothing and could show why.
