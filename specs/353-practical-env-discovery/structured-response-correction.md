# Structured environment response correction

User direction: complete the existing goal; independent coverage and deterministic clean checks remain mandatory.

## Evidence and scope

The template-preflight Elm run held before installation after three structural/contract failures exhausted the existing two schema corrections. The API transport sent only `response_format: json_object`; all phase schemas were merely prompt text. An isolated probe against the same configured GLM endpoint accepted `json_schema` with `strict: true` and obeyed the schema despite an incompatible prompt. A second probe using the complete reviewer/source-tool union also validated. These are formatting capability tests, not coverage or readiness evidence.

Send the current phase's source-tool plus declaration JSON Schema through the API transport. Source inspection permits only source tools; capability preflight adds the probe envelope; construction adds the manifest envelope; independent review adds the assessment envelope. The existing Pydantic contracts supply declaration definitions. No repository-specific language, framework, dependency or filename catalog is introduced.

## Boundaries

The schema is request-local, retained in conductor request parameters and opt-in validation transcripts, and forwarded by the validation recorder as well as normal daemon calls. Ordinary JSON-mode conductor callers retain their existing behavior. Injected chat transports receive the optional schema keyword only when their signature explicitly supports it.

An API rejecting the schema is a provider failure. There is no automatic retry with an unconstrained response format. Existing transport retries, attempt call/read/time/context limits and schema/coverage/execution correction allowances are unchanged. Independent source-bound local validation still rejects malformed or unsupported output, incorrect references, missing obligations and incomplete findings; provider schema compliance alone never confers readiness. No duplicate selection, silent envelope extraction or invented assessment is permitted.

## Verification

Regression tests verify exact wire payloads, request isolation, phase discrimination, preserved source navigation, optional transport compatibility, production discovery forwarding and refusal without downgrade on unsupported endpoints. Replay both saved malformed reviewer responses against the declaration schemas and strict local decoder. Run repository preflight/CI and a fresh Elm-only simulation with the same pinned source, model, low effort and 60-minute experimental profile. Keep other repositories paused until real Elm acceptance; no merge without independent review and all required clean checks.


## Common strict representation

Native OpenAI strict schemas forbid a root union, require every object property and forbid dynamic object properties. The API adapter therefore sends one strict outer `response` property containing the logical phase union. Dynamic string-key maps use arrays of `{key,value}` entries on the wire; all named object fields are required, and logical defaults remain descriptions rather than unsupported schema defaults. A recursive decoder checks exact object fields, scalar types, one matching union branch, JSON-key uniqueness and map-entry uniqueness before restoring the original logical contract. It never guesses between duplicate values. Field schema metadata reflects the existing execution-path grammar; runtime path validation and operator variables retain their previous semantics.

The transport instruction follows the logical schema within the copied system message. Calls retain the compiled wire schema; opt-in validation transcripts retain both wire and decoded responses. Provider errors and invalid wire representations hold closed, without unconstrained fallback. Ordinary conductor JSON calls remain unchanged. Code review verified schema compatibility against [OpenAI's documented restrictions](https://developers.openai.com/api/docs/guides/structured-outputs), all generated object nodes, nesting depth and complete-manifest round trips. Separate live GLM synthetic source, probe, manifest and assessment wire round trips passed; these are not Elm acceptance evidence.
