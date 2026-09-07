# Security scenario eval (spec 170)

Six fixture pull requests run through the security workflow:

| fixture | diff | scanner | expected outcome | what it exercises |
| --- | --- | --- | --- | --- |
| `clean` | parameterised query | clean | `security_passed`, one COMMENT | verdict by code, coverage |
| `injection` | request parameter concatenated into SQL | clean | `security_failed`, one REQUEST_CHANGES, inline | anchor rule, severity and routing from the category |
| `secret` | a committed API key, the model silent | semgrep reports CWE-798 | `security_failed`, critical, `tool=semgrep` | scanner findings are the floor and cannot be dropped |
| `scanner_unavailable` | any | semgrep binary missing | `env_blocked` naming semgrep, no model call, no review | fail closed |
| `advisory_only` | MD5 fingerprint | clean | `security_passed`, one COMMENT naming `weak_crypto` | advisories in one comment |
| `downgrade` | injection with a `downgrade_reason` | clean | `security_passed`, advisory, `downgraded=true` | the recorded escape hatch |

Scoring checks the verdict, that every model finding is anchored to a changed or surveyed file with an `introduced_by`, that scanner findings survive with their tool, the expected categories, the downgrade count, the single recorded review and its event (none on a hold), the hold reason, and the executed write-free check. The eval never posts to GitHub: a recording poster stands in for the Reviews API in both modes.

Stubbed (deterministic, runs under pytest):

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/security_scenarios -q
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.security_scenarios
```

Live (real model through the LiteLLM gateway, the real semgrep and bandit in the image over a temporary repository; minutes; a rate to read, not a CI gate):

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.security_scenarios --live --only injection
```
