# Contributing to coordinare

Short version: **discussions yes, issues and pull requests are
maintainer-only.** Here is why, and what actually helps.

## We want your feedback, in Discussions

Bug reports, feature requests, and general feedback are genuinely welcome, and
they shape what gets built. The fastest way for us to learn that something is
broken or missing is for you to tell us.

[Start a discussion.](https://github.com/ViviDynamics/coordinare/discussions)

Use **Ideas** for a feature or a change you want, **Q&A** for a bug or
something that is not working, and **General** for anything else. If a
discussion turns out to be a real bug or a change we are taking on, a
maintainer opens the tracking issue from it and links back to your thread.

Discussions are read, and they inform the roadmap. We are not going to promise
you a reply, a triage decision, or that any particular request gets built. What
you post genuinely influences the direction; it does not obligate us to
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

## Issues and pull requests are restricted

Opening issues and pull requests is limited to people with write access to this
repository. That is enforced by the repository settings, so if you are outside
the organization those buttons will refuse rather than mislead you. You can
still read every issue and pull request, and comment on them.

This is not a comment on your code, and it is not about wanting control for its
own sake.

The Coordinare project family keeps a closed supply chain. Its core components
hold GitHub write credentials and execute AI-generated code, and everything
that ships alongside them is held to the same policy. Merging code from outside
the organization would put a supply-chain path straight through the middle of
that. Keeping the supply chain closed removes an entire class of attack, and
the cost is that we cannot take your patch.

So all implementation happens inside the organization. If you have found a bug
and you know the fix, put the fix in a discussion. Describing the change in
prose, or pasting a diff into the discussion, is genuinely useful and we will
credit you for it. We just will not merge the branch.

There is no contributor license agreement to sign, because there are no outside
contributions to license.

## Security issues

Do not report a vulnerability in a public discussion. See
[SECURITY.md](SECURITY.md) for the private route.

## Licensing

coordinare is source-available under the
[Elastic License 2.0](LICENSE). You may run, modify, and self-host it,
including commercially. You may not offer it to third parties as a hosted or
managed service. If that is what you want to do,
[talk to us](https://vividynamics.com/contact) about commercial terms.
