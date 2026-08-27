# Quickstart: Verifying the License and Legal Posture

**Feature**: 142-license-and-legal-posture | **Date**: 2026-08-27

How to check that this feature is working, as a reviewer or as the person about to make the
repository public.

## 1. The enforcement checks

```bash
.venv/bin/pytest tests/unit/test_142_license_and_legal_posture.py -v
```

Covers document presence, license declarations in all three `pyproject.toml` files, required and
forbidden wording in the posture files, template presence, the workflow's trigger and permissions
shape, and every distributed dependency against the accepted license set.

Both checks are designed to fail loudly rather than drift. Two ways to confirm they have teeth:

```bash
# Wording guard: temporarily claim coordinare is open source in the README, then revert.
# The guard should fail and name the file and line.

# Dependency check: temporarily add a GPL package to pyproject.toml dependencies and
# re-lock, then revert. The check should fail and name the package and its license.
```

A check that never fails is indistinguishable from one that is not running. Worth doing once
during review rather than trusting the green tick.

## 2. What a reader sees

```bash
ls LICENSE NOTICE CONTRIBUTING.md SECURITY.md
sed -n '/^## License/,/^## /p' README.md
```

Read the README licensing section as a stranger would. It should answer, without ambiguity: may I
run this at work (yes), may I modify it (yes), may I self-host it (yes), may I offer it to my own
customers as a service (no, and here is who to talk to if you want to).

## 3. The dependency audit

```bash
cat specs/142-license-and-legal-posture/dependency-license-audit.md
```

Confirm it is dated, covers both distributed projects, and that every Tier 2 exception carries a
rationale naming how the package is consumed. The audit and the test derive from the same data,
so a disagreement between them is a bug in the derivation rather than a stale document.

To regenerate after a dependency change:

```bash
make lint  # the check runs in the unit suite; regenerate the audit if it fails
```

## 4. The external-contribution automation

This one cannot be verified locally, because it needs a real fork-originated pull request from an
account outside the organization. Verify by inspection plus one live test after merge:

```bash
cat .github/workflows/external-contributions.yml
```

Confirm by reading:
- Trigger is `pull_request_target`, not `pull_request` (a fork `pull_request` gets a read-only
  token and cannot comment, label, or close).
- There is **no** `actions/checkout` step. This is the single most important line to check: the
  workflow holds a write token, and checking out the fork head is what turns
  `pull_request_target` from safe into a privilege-escalation hole.
- `permissions:` grants `pull-requests: write` and nothing more.
- The internal and bot paths exit before any API call, so maintainer pull requests stay silent.
- Security-keyword hits comment and label but do not close.

Live test after merge: have someone outside the organization open a throwaway pull request, or
verify from a personal account. Confirm the comment appears, the label is applied, the pull
request closes, and that your own next pull request produces nothing.

## 5. The pre-public scrub

```bash
cat specs/142-license-and-legal-posture/pre-public-scrub.md
```

Every item records an outcome. Items marked `at_flip` are the ones that cannot be done yet, and
they are the reason a fully ticked checklist does not by itself mean "ready".

**The one that will bite if forgotten**: private vulnerability reporting cannot be enabled on a
private repository, and this repository is private today. `SECURITY.md` points at that route, so
between going public and enabling it, the documented reporting channel does not exist. Enable it
in the GitHub UI in the same sitting as the visibility flip. It needs the UI because the available
tokens cannot write repository administration settings.

## 6. What this feature does not do

Do not expect to find, and do not add here:

| Not here | Owner |
|---|---|
| Threat model, dashboard Origin/Host checks, bind audit | spec 144 / [#198](https://github.com/ViviDynamics/coordinare/issues/198) |
| Removing internal addresses and `spark/*` model ids from examples | spec 145 / [#199](https://github.com/ViviDynamics/coordinare/issues/199) |
| Dashboard authentication | spec 143 / [#197](https://github.com/ViviDynamics/coordinare/issues/197) |
| Forwarding external issues to a private destination | spec 152 / [#210](https://github.com/ViviDynamics/coordinare/issues/210) |
| Making the repository public | a human decision, after the scrub |
