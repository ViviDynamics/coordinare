# Quickstart: Performer Environment Isolation

## Scenario 1 — Commits attributed to bot identity

### Setup
1. Add to `config.yaml`:
   ```yaml
   bot_identity:
     name: coordinare-bot
     email: coordinare-bot@myorg.com
   ```
2. Dispatch a card to the implementer

### Expected
- The performer's commits appear as `coordinare-bot <coordinare-bot@myorg.com>` in git log
- Not the host user's name/email

### Verify
```bash
git log --format="%an <%ae>" origin/<branch> | head -3
# Should output: coordinare-bot <coordinare-bot@myorg.com>
```

---

## Scenario 2 — API key passed through, host secrets not leaked

### Setup
1. Add to `config.yaml`:
   ```yaml
   env_passthrough:
     - ANTHROPIC_API_KEY
   ```
2. Set `ANTHROPIC_API_KEY=sk-ant-...` on the host
3. Set `AWS_SECRET_ACCESS_KEY=secret123` on the host
4. Dispatch a card

### Expected
- The performer subprocess receives `ANTHROPIC_API_KEY`
- The performer subprocess does NOT receive `AWS_SECRET_ACCESS_KEY`
- Logs show `subprocess_transport.env_constructed` with var names (no values)

---

## Scenario 3 — Default behavior (no bot_identity config)

### Setup
1. No `bot_identity` in config.yaml

### Expected
- Performer commits appear as `Coordinare Bot <coordinare@localhost>`
- No behavior change for PAT-only setups

---

## Scenario 4 — Host GITHUB_TOKEN not inherited

### Setup
1. Set `GITHUB_TOKEN=ghp_personal_token` on host (personal PAT)
2. Configure coordinare with a GitHub App (`github_auth: app`)
3. Dispatch a card

### Expected
- The performer subprocess does NOT inherit the host `GITHUB_TOKEN=ghp_personal_token`
- In GitHub App mode, authentication uses the short-lived App installation token delivered via the `dispatch_card` protocol message — no `GITHUB_TOKEN` is set in the subprocess env at spawn time
