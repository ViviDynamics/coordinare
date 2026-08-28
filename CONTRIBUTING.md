# Contributing to coordinare

Short version: **issues yes, pull requests no.** Here is why, and what actually
helps.

## We want your issues

Bug reports, feature requests, and general feedback are genuinely welcome, and
they shape what gets built. The fastest way for us to learn that something is
broken or missing is for you to tell us.

[Open an issue.](https://github.com/ViviDynamics/coordinare/issues/new)

Submissions are read, and they inform the roadmap. We are not going to promise
you a reply, a triage decision, or that any particular request gets built. What
you send genuinely influences the direction; it does not obligate us to
anything, and pretending otherwise would just set you up for disappointment.

### What makes a report useful

For a **bug**: what you expected, what happened instead, the version or commit
you were running, relevant log output, and whether it reproduces or happened
once.

For a **feature request**: tell us the problem before the solution. The problem
is more actionable than the feature, because we may already have a better
answer to it.

For **feedback**: no format needed. Tell us what confused you, what you
expected to exist, or where you gave up.

## We do not accept pull requests

Public pull requests are **not accepted** and are closed unmerged. This is not
a comment on your code, and it is not about wanting control for its own sake.

The Coordinare project family keeps a closed supply chain. Its core
components hold GitHub write credentials and execute AI-generated code, and
everything that ships alongside them is held to the same policy. Merging code
from outside the organization would put a supply-chain path straight through
the middle of that. Keeping the supply chain closed removes an entire class of
attack, and the cost is that we cannot take your patch.

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

coordinare is source-available under the
[Elastic License 2.0](LICENSE). You may run, modify, and self-host it,
including commercially. You may not offer it to third parties as a hosted or
managed service. If that is what you want to do,
[talk to us](https://vividynamics.com/contact) about commercial terms.
