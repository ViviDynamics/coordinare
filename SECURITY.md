# Security Policy

Coordinare is an autonomous agent system: it holds repository write credentials,
executes AI-generated code in containers, and feeds attacker-authored text
(issue bodies, pull request comments, review threads) into model prompts. We
take reports about it seriously, and we would much rather hear from you than
read about it later.

## Reporting a vulnerability

Please report privately, through GitHub's private vulnerability reporting for
this repository:

**https://github.com/ViviDynamics/coordinare/security/advisories/new**

This is the only reporting route. We deliberately publish no email address:
an address that bounces or goes unread is worse than none, and a private
advisory keeps the report where the fix will happen.

Private vulnerability reporting is a feature of public repositories. If the
route above is not available to you, coordinare is not yet public, and the
channel opens when it is.

If you cannot use private advisories at all, open a regular issue asking us to
open a private channel with you, and **do not include any detail about the
vulnerability in it** — not the affected component, not the reproduction, not
the impact. Just ask for the channel.

## What to expect

We read every report. We respond and fix as capacity allows.

We are not going to promise you a schedule. Coordinare is given away free of
charge under a source-available license with no support contract, and a
timeframe we cannot reliably meet would be worth less to you than an honest
statement that we have not committed to one. Nothing here creates an obligation,
and the license disclaims all warranties.

If your own disclosure timeline matters to you, say so in the report and we will
tell you plainly whether we can work to it.

## Releases and fixes

No release of coordinare carries a support commitment, including the most recent
one. Fixes land on `main` when they land. There is no backport process and no
long-term-support line.

If you are running coordinare in an environment where that is not good enough,
run it from a commit you have reviewed and pin it yourself.

## Architecture and trust boundaries

**[docs/security/threat-model.md](docs/security/threat-model.md)** is the full
document, delivered by spec 144
([issue #198](https://github.com/ViviDynamics/coordinare/issues/198)). Read it
before exposing coordinare to any network.

It covers the four trust boundaries, prompt injection as the defining threat
class, the dashboard's unauthenticated posture and what defends it, what the
health endpoints disclose, and a per-feature GitHub token permission matrix.
Every boundary states its residual risk, not just its mitigations.

The short version, if you read nothing else:

- **`docker.sock` is root-equivalent.** Run the daemon on a host you are willing
  to lose. This is an explicit trust decision, not an oversight.
- **The dashboard has no authentication** and is loopback-only by design. A
  localhost guard rejects foreign origins and foreign hosts, but that is a
  locality check, not a login: anyone who can make requests from your machine has
  full control. Authentication is spec 143
  ([#197](https://github.com/ViviDynamics/coordinare/issues/197)).
- **Prompt injection is mitigated structurally, not prevented.** Public issue and
  comment text reaches prompts that hold repository write credentials. The human
  reviewing the pull request is the last reliable line of defence. Read the diff,
  not the summary.
- **Performer output and all GitHub text are untrusted.** The security scan gate
  catches known patterns in changed files, and nothing more.
- **Use a fine-grained token** scoped to one repository plus the board. The
  threat model lists exactly which permission each feature needs.

## Contributions

Coordinare does not accept public pull requests, including security fixes. This
is itself a supply-chain security measure. See
[CONTRIBUTING.md](CONTRIBUTING.md). Report the issue and we will implement the
fix internally, with credit to you in the advisory if you would like it.
