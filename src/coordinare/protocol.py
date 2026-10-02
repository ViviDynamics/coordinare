from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

from pydantic import BaseModel, Field

ActionType = Literal["dispatch", "status", "relay_feedback", "health"]

StatusType = Literal[
    "accepted",
    "working",
    "pr_opened",
    "plan_committed",
    "approved",
    "nothing_to_review",
    "changes_requested",
    "security_passed",
    "nothing_to_scan",
    "not_applicable",
    "security_failed",
    "qa_passed",
    "qa_failed",
    "qa_env_blocked",
    "env_blocked",
    "docs_committed",
    "env_bootstrap_complete",
    "assessment_complete",
    "assessment_not_work",
    "assessment_needs_split",
    "partial_progress",
    "blocked",
    "error",
    "unknown",
    "busy",
    "acknowledged",
    "session_expired",
    "token_limit",
    "healthy",
    "unhealthy",
]

#: Statuses after which the performer's run loop exits and the process is
#: gone -- the transport must not reuse it. Mirror of the break set in
#: agent/performer main.run_loop (412 round 34: including "blocked" -- the
#: session is capped, the transport clears the process either way, and the
#: performer now exits so its graceful cleanup runs inside the reap grace).
PROCESS_EXITING_STATUSES = frozenset({
    "pr_opened",
    "plan_committed",
    "approved",
    "nothing_to_review",
    "changes_requested",
    "security_passed",
    "nothing_to_scan",
    "not_applicable",
    "security_failed",
    "qa_passed",
    "qa_failed",
    "qa_env_blocked",
    "env_blocked",
    "docs_committed",
    "error",
    "blocked",
    # 412 round 8: the run loop also exits on session_expired when no session
    # is active. The transport cannot see whether a session was active, so it
    # clears the process in both cases -- spawning fresh after an expiry is
    # always the safe side.
    "session_expired",
})


class ProtocolMessage(BaseModel):
    action: ActionType
    session_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ProtocolResponse(BaseModel):
    status: StatusType
    session_id: str = ""
    # 412 round 46: session_expired carries this so the transport can tell
    # the no-session exit (process-exiting) from a stale request answered
    # while another session is still being served (not process-exiting).
    active_session: bool = False
    reason: str | None = None
    questions: list[str] = Field(default_factory=list)
    pr_url: str | None = None
    pr_node_id: str | None = None
    progress: str | None = None
    # Telemetry fields — populated by performer on "working" status responses.
    # Must be kept in sync with performer.protocol.PerformerResponse.
    backend: str | None = None  # AGENT_BACKEND name, returned on dispatch
    model: str | None = None  # model override, returned on dispatch if set
    plan_path: str | None = None  # 020: path to committed architecture plan
    comments: list[dict] = Field(default_factory=list)  # 021: review comments [{file, line, body}]
    suggestions: list[str] = Field(default_factory=list)  # 021: non-blocking suggestions
    findings: list[dict] = Field(default_factory=list)  # 022: security findings
    failures: list[dict] = Field(default_factory=list)  # 023: QA failures
    report: dict | None = None  # 023: QA pass report
    files_modified: list[str] = Field(default_factory=list)  # 024: doc files committed
    events: list[dict] = Field(default_factory=list)  # serialised BackendEvent list
    metrics: dict | None = None  # PerformerMetrics (pid, memory_bytes, cpu_percent, …)
    # 063 Phase 4 (T023): performer sets True when services-health.sh exited
    # non-zero during workspace setup. Coordinare's daemon glue routes this
    # into EnvCacheService.mark_runtime_health_failed for forced regen.
    env_cache_health_failed: bool = False
    # 089: performer sets True when the implementer local-test gate ran the
    # detected test_command and it failed for a code reason (not env). The
    # monitor uses this on changes_requested to drive the bounded self-fix loop.
    local_test_failed: bool = False
    # 063 Cross-cutting (T026c/T026d): performer-reported service-inference
    # outcome for env_bootstrap jobs. EnvCacheService stamps these onto
    # EnvCacheState so the dashboard can surface what the agent produced.
    inference_skipped_reason: str | None = None
    inference_agent_version: str | None = None
    inference_attempts: int | None = None
    inference_succeeded: bool | None = None
    inference_services: list[str] = Field(default_factory=list)
    # 070: branch HEAD before/after a performer turn; used by the coordinare
    # router to detect implementer turns that produced zero new commits and
    # route them back to dispatching rather than honoring a "blocked" verdict.
    head_before: str | None = None
    head_after: str | None = None
    # 511: the working branch the performer pushed, set on pr_opened so the
    # 076 artefact write-through records pushed_branch. Must be kept in sync
    # with performer.protocol.PerformerResponse.
    pushed_branch: str | None = None
    # 070: continuation hint emitted with status="partial_progress".
    next_focus: str | None = None
    # 072: number of new PR comments authored by the bot user during this
    # turn. Used by the per-role zero-progress guardrail to distinguish a
    # reviewer that surfaced something real (delta > 0) from one that
    # silently churned (delta == 0).
    bot_pr_comment_delta: int = 0
    # 126: per-item feedback dispositions from the implementer completion
    # contract ({"id", "disposition": "addressed"|"disputed", "reason"}).
    # Consumed by monitor_performer's terminal-success floor; absent/empty
    # means "nothing disputed" (backward compatible with older performers).
    feedback_dispositions: list[dict] = Field(default_factory=list)


# 412 round 15: the hand-tightened message-contract strictness -- the
# action-specific payload shapes the raw Pydantic schema cannot express.
# Applied to every generated message contract (see generate_contracts).
MESSAGE_SCHEMA_PATCH = {
    "$defs": {
        "dispatch_payload": {
            "title": "DispatchPayload",
            "description": "Payload for action=dispatch: one of the dispatch variants (card, env-bootstrap job, wiki-init).",
            "type": "object",
            "anyOf": [
                {
                    "title": "CardDispatchPayload",
                    "description": "Card dispatch. Contains the full card context from the board pickup.",
                    "type": "object",
                    "required": [
                        "id",
                        "title",
                        "description",
                        "acceptance_criteria",
                        "status",
                    ],
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Board item node ID (e.g. PVT_kwDOABCDEF)",
                        },
                        "title": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "description": {
                            "type": "string",
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "items": {
                                "type": "string",
                            },
                        },
                        "status": {
                            "type": "string",
                            "description": "Board column name at time of dispatch (e.g. 'In Progress')",
                        },
                        "previous_status": {
                            "type": "string",
                        },
                        "issue_id": {
                            "type": "string",
                        },
                        "issue_number": {
                            "type": "integer",
                        },
                        "issue_url": {
                            "type": "string",
                        },
                        "github_token": {
                            "type": [
                                "string",
                                "null",
                            ],
                            "description": "GitHub token for the performer to act with.",
                        },
                        "assigned_agent": {
                            "type": [
                                "string",
                                "null",
                            ],
                            "default": None,
                        },
                    },
                },
                {
                    "title": "BootstrapDispatchPayload",
                    "description": "Env-bootstrap job dispatch (coordinare/models/env_cache.py: BootstrapJobPayload).",
                    "type": "object",
                    "required": [
                        "job_type",
                        "symphony_name",
                        "symphony_org",
                        "symphony_repo",
                    ],
                    "properties": {
                        "job_type": {
                            "type": "string",
                            "const": "env_bootstrap",
                        },
                        "symphony_name": {
                            "type": "string",
                        },
                        "symphony_org": {
                            "type": "string",
                        },
                        "symphony_repo": {
                            "type": "string",
                        },
                        "env_spec_files": {
                            "type": "array",
                            "items": {
                                "type": "string",
                            },
                        },
                        "env_spec_contents": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "string",
                            },
                        },
                        "cache_mount_path": {
                            "type": "string",
                        },
                        "last_failure": {
                            "type": [
                                "string",
                                "null",
                            ],
                        },
                    },
                },
                {
                    "title": "WikiInitDispatchPayload",
                    "description": "Cardless wiki-init dispatch (daemon._execute_wiki_init_dispatch).",
                    "type": "object",
                    "required": [
                        "card_id",
                        "title",
                        "description",
                    ],
                    "properties": {
                        "card_id": {
                            "type": "string",
                        },
                        "title": {
                            "type": "string",
                        },
                        "description": {
                            "type": "string",
                        },
                        "role": {
                            "type": "string",
                        },
                        "doc_mode": {
                            "type": "string",
                        },
                        "repo_url": {
                            "type": "string",
                        },
                        "branch": {
                            "type": "string",
                        },
                        "base_branch": {
                            "type": "string",
                        },
                    },
                },
            ],
        },
        "relay_feedback_payload": {
            "title": "RelayFeedbackPayload",
            "description": "Payload for action=relay_feedback. The session ID rides the outer message (412 round 17: aligned with the transport caller). The cancellation path sends no PR context, so only ``comments`` is required and ``pr_url`` accepts the empty string the serializer produces (412 round 32).",
            "type": "object",
            "required": [
                "comments",
            ],
            "properties": {
                "pr_url": {
                    "type": "string",
                    "description": "URL of the pull request under review; empty for the cancellation relay.",
                },
                "comments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "author_login",
                            "body",
                        ],
                        "properties": {
                            "author_login": {
                                "type": "string",
                                "description": "Identity of the reviewer (e.g. 'copilot', 'human:jdoe')",
                            },
                            "body": {
                                "type": "string",
                                "description": "Comment text.",
                            },
                            "file": {
                                "type": [
                                    "string",
                                    "null",
                                ],
                                "default": None,
                                "description": "File path the comment applies to, if applicable.",
                            },
                            "line": {
                                "type": [
                                    "integer",
                                    "null",
                                ],
                                "default": None,
                                "description": "Line number the comment applies to, if applicable.",
                            },
                        },
                    },
                },
            },
        },
        "status_payload": {
            "title": "StatusPayload",
            "description": "Payload for action=status and action=health: empty, or a mid-session token refresh.",
            "type": "object",
            "properties": {
                "github_token": {
                    "type": "string",
                    "description": "Optional fresh GitHub token for the performer to re-authenticate with (App tokens expire).",
                },
            },
            "additionalProperties": False,
        },
    },
}


def generate_contracts(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    response_doc = ProtocolResponse.model_json_schema()
    # 412 round 14: keep the strictness the hand-tightened contract carried --
    # the raw Pydantic schema is permissive (extras allowed, no URI format),
    # which weakens the published contract for external implementors.
    # 412 round 18: restore the v1 envelope the hand schema carried --
    # identity ($schema/$id/title/description/$comment), the required key,
    # and the questions minLength -- so the published contract keeps its
    # semantics for external implementors.
    response_doc["additionalProperties"] = False
    response_doc["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    response_doc["$id"] = "coordinare/protocol-response/v1"
    response_doc["title"] = "ProtocolResponse"
    response_doc["description"] = (
        "A structured reply from an agent to the coordinare. Serialised as JSON "
        "to the agent process stdout (SubprocessTransport) or equivalent. This "
        "schema is the single source of truth for both the coordinare's response "
        "validator and external agent implementors."
    )
    response_doc["$comment"] = (
        "Status semantics: accepted=dispatch accepted; working=actively "
        "processing; pr_opened=PR created (pr_url+pr_node_id required); "
        "blocked=needs human input (questions required); error=unrecoverable "
        "failure (reason required); unknown=session not recognised; "
        "busy=cannot accept new work; acknowledged=relay_feedback received; "
        "session_expired=session context lost (reason required); "
        "active_session=true on session_expired means another session is "
        "still being served (transport keeps the live process). The 412 "
        "reviewer-security work extends the enum with stage-advance and "
        "terminal statuses (see the enum for the full list)."
    )
    response_doc["required"] = ["status"]
    questions = response_doc.get("properties", {}).get("questions")
    if questions is not None:
        items = questions.setdefault("items", {})
        if isinstance(items, dict):
            items["minLength"] = 1
        questions["description"] = "Questions requiring human input. Must be non-empty when status is blocked."
        questions["default"] = []
    pr_url = response_doc.get("properties", {}).get("pr_url")
    if pr_url is not None:
        # 412 round 18: the URI format belongs on the string branch -- the
        # property level format does not constrain a nullable anyOf.
        for branch in pr_url.get("anyOf", []):
            if isinstance(branch, dict) and branch.get("type") == "string":
                branch["format"] = "uri"
    (output_dir / "protocol-response.schema.json").write_text(
        json.dumps(response_doc, indent=2) + "\n",
    )
    message_schema = ProtocolMessage.model_json_schema()
    # 412 round 15: the message contract's action-specific payload $defs are
    # a code-level transformation, not a function of the destination file --
    # generation stays deterministic and a future model change cannot leave
    # the checked-in contract silently stale.
    defs = MESSAGE_SCHEMA_PATCH["$defs"]
    message_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    message_schema["$id"] = "coordinare/protocol-message/v1"
    message_schema["description"] = (
        "A structured command sent by the coordinare to an agent via any "
        "transport. Transport-agnostic; serialised as JSON to stdin of the "
        "agent process (SubprocessTransport) or equivalent."
    )
    message_schema["required"] = ["action", "session_id", "payload"]
    message_schema["additionalProperties"] = False
    message_schema.setdefault("$defs", {}).update(defs)
    # 412 round 18: the payload shape is action-DISCRIMINATED -- a bare oneOf
    # on the payload property lets an empty dispatch payload validate through
    # the empty alternative and rejects a real status token refresh. The
    # message-level if/then ties each alternative to its action.
    message_schema["properties"]["payload"] = {
        "title": "Payload",
        "description": "Action-specific data. Shape varies by action (see dispatch_payload, relay_feedback_payload and status_payload schemas).",
        "default": {},
        "type": "object",
    }
    message_schema["allOf"] = [
        {
            "if": {"properties": {"action": {"const": action}}},
            "then": {"properties": {"payload": {"$ref": f"#/$defs/{def_name}"}}},
        }
        for action, def_name in (
            ("dispatch", "dispatch_payload"),
            ("relay_feedback", "relay_feedback_payload"),
            ("status", "status_payload"),
            ("health", "status_payload"),
        )
    ]
    (output_dir / "protocol-message.schema.json").write_text(
        json.dumps(message_schema, indent=2) + "\n",
    )
