# Later nare candidates

This inventory changes no runtime outside bootstrap. It is not authorization to
migrate these components or expand their tools.

| Candidate | Existing behavior and duplication | Possible benefit | Risk and acceptance before adoption |
| --- | --- | --- | --- |
| Service inference, first | `packages/service_inference/.../agent.py` owns a bounded model/tool loop, step timeouts and cumulative usage; `openai_compat_llm_client.py` translates tool arguments, and package orchestration owns retries. | Reuse native tool dispatch, compaction, checkpoint and usage machinery while retaining the service manifest validator. | Preserve read-only repository tools, secret boundaries, manual overrides, service probing, iteration/tool budgets and exact manifest admission. Test real provider continuation and malformed tool arguments; compare successful outputs and resource cleanup before removing its loop. |
| Configuration assistant, next | `services/config_assistant.py` deliberately uses one structured response per operator turn. Schema validation and draft/apply authorization are separate from the backend. | A typed one-shot adapter may share endpoint binding, accounting and terminal reporting. Native evidence tools could help only if a later requirement establishes a need. | Do not replace its single-shot contract merely to use an agent. Preserve draft validation, operator approval, sensitive field rules and its model policy. Compare normal/error responses and prove no configuration write can occur through a model tool. |
| Observer, next | `services/observer.py` makes a bounded structured judgment over monitoring evidence. Its parser and retune policy own allowed consequences. | Shared one-shot accounting and model configuration may reduce duplicated failure handling. | Preserve observation/retune authority, cooldowns, missing-evidence behavior and the existing timeout. A session must not invent observations or acquire execution tools. Prove identical decisions under the existing evidence matrix before adoption. |

Keep ordinary classifiers as one-shot calls unless a demonstrated task requires
iterative evidence collection. Shared runtime mechanics do not transfer domain
permission, execution authority or readiness between these components.
