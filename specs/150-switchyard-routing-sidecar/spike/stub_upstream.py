"""A stub OpenAI-compatible upstream, so the escalation route can be observed.

The spike's question is behavioural: does Switchyard's escalation router do what
its documentation says, and does it report what it says it reports? Answering that
needs a upstream whose replies are chosen by the test, not a model whose replies
are chosen by luck — and it must record which target was asked for, since that is
the observation the whole exercise is about.

Every request is logged as one JSON line: model asked for, and the message count.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# Replies are keyed by the model the router selected, so a transcript of calls
# tells you which tier served each turn without inferring it from content.
REPLIES = {
    "weak": "weak-model-reply",
    "strong": "strong-model-reply",
}
#: The judge must answer in the escalation verdict schema. Always escalating makes
#: the latch fire as fast as `confirmations` allows, which is what we want to time.
#: The field name matters and is not guessable: a reply missing `escalate` is
#: discarded with a WARN and the turn routes as if no judge had run. Discovered by
#: getting it wrong first — see findings.md F8.
JUDGE_VERDICT = '{"escalate": true, "reason": "stub judge always escalates"}'


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = str(body.get("model", ""))
        messages = body.get("messages") or []

        print(
            json.dumps({"model": model, "messages": len(messages)}),
            file=sys.stderr,
            flush=True,
        )

        if "judge" in model:
            content = JUDGE_VERDICT
        else:
            content = next(
                (v for k, v in REPLIES.items() if k in model), f"reply-from-{model}"
            )

        payload = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        """Quiet: the JSON line above is the record, and mixing them is noise."""


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    # Binds all interfaces because the sidecar runs in a container and reaches the
    # host through host.docker.internal, which a loopback bind will not accept.
    # Acceptable for a throwaway local spike serving canned text; it is not a
    # pattern to copy into anything that ships.
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
