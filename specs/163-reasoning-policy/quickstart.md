# Reasoning policy and truncation classification

A response that exhausts its output budget is reported as a token-limit block for every role. Raise the role's output budget or shorten the prompt before retrying. Truncation is not retried with an unchanged request. Normal empty or malformed responses retain their existing retry and environment-block behavior.

A self-hosted model endpoint can explicitly opt in:

```yaml
model_endpoints:
  - name: measured-qwen
    endpoint: local-litellm
    model: ada/qwen3-14b
    reasoning_policy: disable_thinking
```

The default is no policy. Without an opt-in, routing and requests stay as before. The option is a per-model property, including separate planner/executor models; it is not a global or role setting. Unknown policy values and native vendor endpoints reject this option at config load.

Policy-enabled CLI traffic uses the canonical proxy, which accepts Messages, Chat Completions and Responses. Workflow model calls add the same extension directly, while workflow CLI turns share the proxy. Existing routing target auth and response repairs are retained.

Research found the option beneficial on `ada/qwen3-14b` and `ada/qwen3-8b`. It was harmful on `spark/glm-5.3-flash`: thinking moved into the answer and made it unparsable twice. GLM opt-in is rejected. See `policy-evidence.json` for the distinction between harmful, beneficial and unmeasured; measure a model before opting it in. No shipped model configuration is enabled by this feature, and no gateway deployment change is required.
