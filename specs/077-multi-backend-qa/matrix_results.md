# Browser-control matrix — consolidated results

_105 cells, 15 models x 7 backends. ✅ = drove Chrome → valid PNG; ❌ = no valid PNG; - = not run._

| model | claude_code | codex | hermes | junie | openclaw | opencode | pi | pass |
|---|---|---|---|---|---|---|---|---|
| `deepcoder:14b` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 0/7 |
| `deepseek-coder-v2:16b` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 0/7 |
| `glm-4.7-flash:latest` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 7/7 |
| `gpt-oss:120b` | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | 6/7 |
| `gpt-oss:20b` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 7/7 |
| `laguna-xs.2:latest` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 7/7 |
| `llama3.1:70b-instruct-q4_K_M` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 0/7 |
| `llama3.3-70b-iq4xs:latest` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 0/7 |
| `llama3.3-70b-q4ks:latest` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 0/7 |
| `llama3.3:70b` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 0/7 |
| `qwen2.5-coder:14b-instruct-q6_K` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 0/7 |
| `qwen2.5:14b-instruct-q6_K` | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | 2/7 |
| `qwen2.5:32b` | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ | ✅ | 4/7 |
| `qwen3-coder:30b` | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 6/7 |
| `qwen3.6:35b` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 7/7 |

## Per-backend (valid PNG across models)
- **claude_code**: 5/15
- **codex**: 5/15
- **hermes**: 7/15
- **junie**: 8/15
- **openclaw**: 7/15
- **opencode**: 7/15
- **pi**: 7/15
