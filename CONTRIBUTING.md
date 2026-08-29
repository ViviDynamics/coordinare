# Contributing to coordinare

Short version: **issues yes, pull requests no.** Here is why, and what actually
helps.

## We want your issues

Bug reports, feature requests, and general feedback are genuinely welcome, and
they shape what gets built. Coordinare is developed by a small team, and the
fastest way for us to learn that something is broken or missing is for you to
tell us.

[Open an issue.](https://github.com/ViviDynamics/coordinare/issues/new/choose)
There are separate forms for bugs, feature requests, and open-ended feedback.

When you open an issue it is forwarded to the maintainers directly, so nothing
depends on someone happening to be watching the repository that day. That is a
statement about delivery, not about response: see below.

Submissions are read, and they inform the roadmap. We are not going to promise
you a reply, a triage decision, or that any particular request gets built. What
you send genuinely influences the direction; it does not obligate us to
anything, and pretending otherwise would just set you up for disappointment.

### What makes a report useful

For a **bug**, the things that save us the most time:

- What you expected, and what happened instead.
- The coordinare version or commit, plus how you are running it (local, Docker).
- Which performer roles and which model backend were involved. Coordinare is a
  multi-agent system, and "the implementer failed" and "the assessor failed" are
  usually unrelated problems.
- Relevant log output. Structured log lines with the card or session identifier
  are worth more than a screenshot of a terminal.
- Whether it reproduces, or happened once.

For a **feature request**, tell us the problem before the solution. "I cannot
tell whether a stuck card is working or wedged" is more actionable than "add a
status column", because we may already have a better answer to the first.

For **feedback**, no format needed. Tell us what confused you, what you expected
to exist, or where you gave up. Where people abandon the setup is some of the
most valuable information we get.

## We do not accept pull requests

Public pull requests are **not accepted** and are closed unmerged. This is not a
comment on your code, and it is not about wanting control for its own sake.

Coordinare is an autonomous agent system that holds GitHub write credentials,
executes AI-generated code, and feeds public text into model prompts. Merging
code from outside the organization would put a supply-chain path straight
through the middle of that. Keeping the supply chain closed removes an entire
class of attack, and the cost is that we cannot take your patch.

So all implementation happens inside the organization. If you have found a bug
and you know the fix, put the fix in the issue. Describing the change in prose,
or pasting a diff into the issue body, is genuinely useful and we will credit
you for it. We just will not merge the branch.

There is no contributor license agreement to sign, because there are no outside
contributions to license.

## Security issues

Do not open a public issue for a vulnerability. See
[SECURITY.md](SECURITY.md) for the private route.

## Licensing

Coordinare is source-available under the [Elastic License 2.0](LICENSE). You may
run, modify, and self-host it, including commercially. You may not offer it to
third parties as a hosted or managed service. If that is what you want to do,
[talk to us](https://vividynamics.com/contact) about commercial terms.
