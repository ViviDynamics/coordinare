# 107: Performer security floor and no daemon clone

Coordinare decides and gates; performers work on the target repository.
PR 115 removed redundant container checkouts except the prose security floor.
This change moves that floor into the security performer.

## Acceptance

1. Only the development subprocess transport uses a daemon checkout.
   `WorkspaceManager.prepare` must run no git and create no clone directory for
   every other transport, including when `local_clone=True` is requested.
2. Prose security jobs run semgrep and bandit once in the performer checkout
   before starting the backend. The existing normalization, severity thresholds
   and routing apply. Runtime findings are supplied to the model and returned
   in the response, independently of model output.
3. The daemon accepts only supported, complete, structurally valid scan evidence
   from the current response. Missing evidence, tool failures, malformed output,
   unreadable targets, failed diff reads and path overflow hold the stage closed.
   State from a prior dispatch cannot satisfy this gate.
4. High or critical scanner findings override a model pass and route repairs.
   Scanner evidence replaces a model downgrade of the same finding.
5. A prose model cannot bypass the floor by emitting a workflow report key.
   Explicitly configured security workflows keep their existing performer scan.
6. An old performer without the response contract holds closed. An old daemon
   without the request contract also causes the new prose performer to hold.

## Wire contract

The daemon sends `security_floor` in card context and HTTP metadata. Version 1
contains `files` from the GitHub changed path list and `diff_status`. A delivered
empty diff is distinct from an unavailable diff. The existing path cap applies;
its overflow holds rather than scanning a truncated set.

The performer returns `report.security_floor` with integer `version: 1`, boolean
`complete`, `tools: [semgrep, bandit]` and normalized `findings`. Only runtime
code creates this evidence. Failure reports contain a payload free reason and
`complete: false`. Empty findings are valid only in a complete report. A
successfully delivered empty changed path list needs no scanner subprocess.

## Deploy

Rebuild and deploy the performer images and daemon together. Prose security
images must contain semgrep and bandit; the full image already installs both.
Mixed versions hold security work until both ends are upgraded and the job is
redispatched. No configuration or persisted state migration is required.
Subprocess remains development only and retains its local checkout.
