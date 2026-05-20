"""Pluggable card assessment backends for the assess_card graph node."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration
    from coordinare.services.claude import ClaudeService

log = structlog.get_logger(__name__)

def _build_assess_prompt(card: dict[str, Any]) -> str:
    """Build a context-aware assessment prompt from card data.

    Accepts both raw issue dicts (from get_issue_details, which includes url,
    labels, and comments) and card dicts stored in state (which use 'description'
    instead of 'body').  Inlines all available context so the model can ask
    targeted questions and avoid repeating ones already answered.

    If card contains a non-empty ``persona_instructions`` key, those instructions
    are prepended as a ``## Assessor Instructions`` section (018-performer-personas).
    """
    # Persona instructions prefix (018-performer-personas)
    persona_prefix = ""
    persona_instructions = str(card.get("persona_instructions", "") or "").strip()
    if persona_instructions:
        persona_prefix = f"## Assessor Instructions\n{persona_instructions}\n\n"

    title = str(card.get("title", "")).strip()
    body = str(card.get("body", "") or card.get("description", "")).strip()
    url = str(card.get("url", "")).strip()
    clarifications: list[dict] = card.get("clarifications", []) if isinstance(card, dict) else []

    # Labels
    labels_node = card.get("labels")
    label_names: list[str] = []
    if isinstance(labels_node, dict):
        label_names = [
            str(n.get("name", "")) for n in labels_node.get("nodes", [])
            if isinstance(n, dict) and n.get("name")
        ]
    elif isinstance(labels_node, list):
        label_names = [str(lbl) for lbl in labels_node if lbl]

    # Existing comments written by humans (exclude coordinare "Needs input:" comments)
    comments_node = card.get("comments")
    human_comments: list[str] = []
    if isinstance(comments_node, dict):
        for c in comments_node.get("nodes", []):
            if not isinstance(c, dict):
                continue
            body_text = str(c.get("body", "")).strip()
            author = str(c.get("author", {}).get("login", "") if isinstance(c.get("author"), dict) else "")
            if body_text and not body_text.startswith("Needs input:"):
                human_comments.append(f"  [{author}]: {body_text}")

    # Build the card section
    lines = [f"Title: {title}"]
    if url:
        lines.append(f"URL: {url}")
    if label_names:
        lines.append(f"Labels: {', '.join(label_names)}")
    lines.append(f"Description:\n{body}" if body else "Description: (none provided)")
    if human_comments:
        lines.append("Existing comments:\n" + "\n".join(human_comments))
    card_text = "\n".join(lines)

    if clarifications:
        history_lines = []
        for entry in clarifications:
            qs = entry.get("questions") or []
            ans = str(entry.get("answer", "")).strip()
            if qs:
                history_lines.append("Questions asked:\n" + "\n".join(f"  - {q}" for q in qs))
            if ans:
                history_lines.append(f"User answered:\n  {ans}")
        history_text = "\n".join(history_lines)
        return (
            persona_prefix
            + "You are reviewing a software feature card to determine if it has enough detail to implement.\n\n"
            f"{card_text}\n\n"
            f"Clarification conversation so far:\n{history_text}\n\n"
            "Based on all the above, decide if there is now sufficient information to implement this feature.\n"
            "If yes, set sufficient=true.\n"
            "If information is still missing, generate 3-5 specific follow-up questions targeting exactly "
            "what is still unclear — do not repeat questions already answered, and reference details from "
            "the card or prior answers in your questions.\n\n"
            'Return only valid JSON: {"sufficient": bool, "questions": [str], "rationale": str}'
        )

    return (
        persona_prefix
        + "You are reviewing a software feature card to determine if it has enough detail to implement.\n\n"
        f"{card_text}\n\n"
        "Check for: clear requirements, defined scope, affected components or routes, acceptance criteria, "
        "and technical constraints or edge cases.\n"
        "If the card has enough information to begin implementation, set sufficient=true.\n"
        "If critical information is missing, set sufficient=false and generate 3-5 specific, concrete "
        "questions that would directly unblock a developer. Reference the actual feature title and any "
        "details from the card in your questions — do NOT ask generic questions like "
        "'What are the requirements?' or 'Please provide more details'.\n\n"
        'Return only valid JSON: {"sufficient": bool, "questions": [str], "rationale": str}'
    )


def _parse_assessment_response(text: str) -> dict[str, Any]:
    """Parse a model text response into an assessment dict.

    Tries several strategies in order:
    1. Direct JSON parse of the full text.
    2. Strip markdown code fences and parse the inner block.
    3. Extract the first JSON object containing "sufficient" via regex.
    4. Keyword heuristic as a last resort.
    """
    candidates: list[str] = [text.strip()]

    # Strategy 2: strip code fences
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = "\n".join(lines[1:-1]) if len(lines) > 2 else stripped
        candidates.append(inner.strip())

    # Strategy 3: find JSON object containing "sufficient" anywhere in the text
    match = re.search(r'\{[^{}]*"sufficient"[^{}]*\}', text, re.DOTALL)
    if match:
        candidates.append(match.group())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "sufficient" in parsed:
                result: dict[str, Any] = {
                    "sufficient": bool(parsed.get("sufficient", False)),
                    "questions": list(parsed.get("questions", [])),
                    "rationale": str(parsed.get("rationale", text)),
                }
                # 046: Pass through assessor-detected dependencies so
                # assess_card can block cards with implicit blockers.
                raw_deps = parsed.get("dependencies")
                if isinstance(raw_deps, list) and raw_deps:
                    result["dependencies"] = raw_deps
                return result
        except (json.JSONDecodeError, AttributeError, ValueError):
            continue

    # Strategy 4: keyword heuristic
    lowered = text.lower()
    sufficient = (
        '"sufficient": true' in lowered
        or '"sufficient":true' in lowered
        or "sufficient: true" in lowered
    )
    if sufficient:
        return {"sufficient": True, "questions": [], "rationale": text}
    # Return empty questions — handle_blocked will generate them from the card title
    return {"sufficient": False, "questions": [], "rationale": text}


_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _parse_prompt_response(text: str, response_format: str | None) -> dict[str, Any]:
    """Build a prompt response dict, optionally parsing JSON from the text.

    When ``response_format="json"``, tries multiple strategies:
    1. Parse the full text as JSON.
    2. Extract the first code fence (labeled or unlabeled) and parse its contents.
    3. Log a warning if all strategies fail.
    """
    text = text.strip() if text else ""
    result: dict[str, Any] = {"text": text, "data": None}
    if response_format == "json" and text:
        # Strategy 1: full text is JSON
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            result["data"] = json.loads(text)
        # Strategy 2: extract JSON from code fence anywhere in text
        if result["data"] is None:
            match = _CODE_FENCE_RE.search(text)
            if match:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    result["data"] = json.loads(match.group(1).strip())
        if result["data"] is None:
            log.warning("prompt_response_json_parse_failed", text_length=len(text))
    return result


@runtime_checkable
class ConductingBackend(Protocol):
    async def assess(self, card: dict[str, Any]) -> dict[str, Any]: ...
    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]: ...


class AnthropicApiBackend:
    """Delegates to ClaudeService — uses the Anthropic SDK directly."""

    def __init__(self, claude_service: ClaudeService) -> None:
        self._svc = claude_service

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        return await self._svc.assess_card_sufficiency(card)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """Send an arbitrary text prompt to the Anthropic API."""
        if not text.strip():
            return {"text": "", "data": None}
        raw = await self._svc.prompt_text(text, response_format=response_format)
        return _parse_prompt_response(raw, response_format)


class OpenAiApiBackend:
    """Direct OpenAI Chat Completions call via httpx — no SDK dep required.

    Supports any OpenAI-compatible endpoint (Azure, OpenRouter, local proxies)
    by setting ``base_url``.  Auth comes from OPENAI_API_KEY.
    """

    _TIMEOUT: float = 60.0

    def __init__(
        self,
        api_key: str | None,
        model: str = "gpt-4o-mini",
        max_tokens: int = 4096,
        temperature: float | None = None,
        base_url: str | None = None,
        effort: str | None = None,
    ) -> None:
        self._api_key = api_key or ""
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._effort = effort
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

    async def _chat(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        import httpx
        if not self._api_key:
            log.error("openai_api_missing_key")
            return ""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        # Reasoning models (o-series / gpt-5) reject `max_tokens` and `temperature`
        # and require `max_completion_tokens`. Use the presence of `effort` as the
        # signal — the config layer ties `effort` to reasoning-capable models.
        if self._effort is not None:
            payload["max_completion_tokens"] = self._max_tokens
            payload["reasoning_effort"] = self._effort
        else:
            payload["max_tokens"] = self._max_tokens
            if self._temperature is not None:
                payload["temperature"] = self._temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._TIMEOUT) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            log.error("openai_api_http_error", error=str(exc))
            return ""
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            log.warning("openai_api_unexpected_response", keys=list(data.keys()) if isinstance(data, dict) else None)
            return ""

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        prompt = _build_assess_prompt(card)
        text = await self._chat(
            [
                {"role": "system", "content": "Respond ONLY with valid JSON. No prose, markdown, or code fences."},
                {"role": "user", "content": prompt},
            ],
            json_mode=True,
        )
        if not text:
            return {"sufficient": False, "questions": [], "rationale": "empty openai response"}
        return _parse_assessment_response(text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        if not text.strip():
            return {"text": "", "data": None}
        messages: list[dict[str, str]] = []
        if response_format == "json":
            messages.append({"role": "system", "content": "Respond with valid JSON only."})
        messages.append({"role": "user", "content": text})
        raw = await self._chat(messages, json_mode=(response_format == "json"))
        return _parse_prompt_response(raw, response_format)


class ClaudeCliBackend:
    """Runs `claude --print <prompt>` as a subprocess using local CLI auth."""

    _TIMEOUT: int = 30

    def __init__(self, executable: str = "claude") -> None:
        self._executable = executable

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        # Prompt is piped via stdin (not argv) so card text — which may include
        # user-authored issue bodies and comments — stays out of process
        # listings and avoids argv length limits on long clarification chains.
        prompt = _build_assess_prompt(card)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                "--print",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=self._TIMEOUT
            )
        except TimeoutError:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
            log.warning("assessment_cli_timeout", timeout=self._TIMEOUT)
            return {"sufficient": False, "questions": [], "rationale": "cli timeout"}
        except OSError as exc:
            log.error("assessment_cli_launch_failed", error=str(exc))
            return {"sufficient": False, "questions": [], "rationale": f"cli error: {exc}"}

        text = stdout.decode(errors="replace").strip() if stdout else ""
        if not text:
            return {"sufficient": False, "questions": [], "rationale": "empty cli response"}
        return _parse_assessment_response(text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """Send an arbitrary text prompt via the Claude CLI via stdin."""
        if not text.strip():
            return {"text": "", "data": None}
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                "--print",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=text.encode()), timeout=self._TIMEOUT
            )
        except TimeoutError:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
            log.warning("prompt_cli_timeout", timeout=self._TIMEOUT)
            return {"text": "", "data": None}
        except OSError as exc:
            log.error("prompt_cli_launch_failed", error=str(exc))
            return {"text": "", "data": None}

        if proc.returncode and proc.returncode != 0:
            err = stderr.decode(errors="replace").strip() if stderr else ""
            log.warning("prompt_cli_nonzero_exit", returncode=proc.returncode, stderr_length=len(err))

        raw = stdout.decode(errors="replace").strip() if stdout else ""
        return _parse_prompt_response(raw, response_format)


class OpenCodeBackend:
    """Runs `opencode run --print <prompt>` as a subprocess.

    Uses the same opencode auth already configured for the performer.
    """

    _TIMEOUT: int = 60

    def __init__(self, executable: str = "opencode", effort: str | None = None) -> None:
        self._executable = executable
        self._effort = effort

    def _effort_args(self) -> list[str]:
        return ["--effort", self._effort] if self._effort else []

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        # Prompt is piped via stdin (not argv); see ClaudeCliBackend.assess.
        prompt = _build_assess_prompt(card)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                "run",
                *self._effort_args(),
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=self._TIMEOUT
            )
        except TimeoutError:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
            log.warning("assessment_opencode_timeout", timeout=self._TIMEOUT)
            return {"sufficient": False, "questions": [], "rationale": "opencode timeout"}
        except OSError as exc:
            log.error("assessment_opencode_launch_failed", error=str(exc))
            return {"sufficient": False, "questions": [], "rationale": f"opencode error: {exc}"}

        text = stdout.decode(errors="replace").strip() if stdout else ""
        if not text:
            return {"sufficient": False, "questions": [], "rationale": "empty opencode response"}
        return _parse_assessment_response(text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """Send an arbitrary text prompt via the opencode CLI via stdin."""
        if not text.strip():
            return {"text": "", "data": None}
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                "run",
                *self._effort_args(),
                "-",  # read from stdin
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=text.encode()), timeout=self._TIMEOUT
            )
        except TimeoutError:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
            log.warning("prompt_opencode_timeout", timeout=self._TIMEOUT)
            return {"text": "", "data": None}
        except OSError as exc:
            log.error("prompt_opencode_launch_failed", error=str(exc))
            return {"text": "", "data": None}

        if proc.returncode and proc.returncode != 0:
            err = stderr.decode(errors="replace").strip() if stderr else ""
            log.warning("prompt_opencode_nonzero_exit", returncode=proc.returncode, stderr_length=len(err))

        raw = stdout.decode(errors="replace").strip() if stdout else ""
        return _parse_prompt_response(raw, response_format)


class CodexCliBackend:
    """Runs `codex exec` as a subprocess — pairs with the codex-backed performers.

    Uses the same codex CLI (and OPENAI_API_KEY) the performer fleet already
    relies on, so no new auth is required.
    """

    _TIMEOUT: int = 60

    def __init__(
        self,
        executable: str = "codex",
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        self._executable = executable
        self._model = model
        self._effort = effort

    def _build_args(self) -> list[str]:
        # Order: subcommand → options → positional stdin sentinel.
        # `codex exec` parses `[PROMPT]` positionally; `-` means "read from
        # stdin." Flags must precede the positional so clap doesn't bind them
        # past the sentinel. The -c value is passed unquoted — codex parses it
        # as TOML and falls back to a literal string when TOML parse fails.
        args = [self._executable, "exec"]
        if self._model:
            args.extend(["--model", self._model])
        if self._effort:
            args.extend(["-c", f"model_reasoning_effort={self._effort}"])
        args.append("-")
        return args

    async def _run(self, prompt: str) -> str:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_args(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=self._TIMEOUT
            )
        except TimeoutError:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
            log.warning("codex_cli_timeout", timeout=self._TIMEOUT)
            return ""
        except OSError as exc:
            log.error("codex_cli_launch_failed", error=str(exc))
            return ""
        if proc.returncode and proc.returncode != 0:
            err = stderr.decode(errors="replace").strip() if stderr else ""
            log.warning("codex_cli_nonzero_exit", returncode=proc.returncode, stderr_length=len(err))
        return stdout.decode(errors="replace").strip() if stdout else ""

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        text = await self._run(_build_assess_prompt(card))
        if not text:
            return {"sufficient": False, "questions": [], "rationale": "empty codex response"}
        return _parse_assessment_response(text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        if not text.strip():
            return {"text": "", "data": None}
        raw = await self._run(text)
        return _parse_prompt_response(raw, response_format)


class NullBackend:
    """Skips assessment — assumes all cards are sufficient."""

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        return {"sufficient": True, "questions": [], "rationale": "assessment disabled"}

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """No-op prompt — returns empty text."""
        return {"text": "", "data": None}


def build_conducting_backend(
    config: ProjectConfiguration,
    circuit_breaker: Any = None,
    retry_kwargs: dict[str, Any] | None = None,
) -> ConductingBackend:
    """Factory: construct the configured conducting backend.

    Reads ``config.conducting``.  Model / max_tokens / temperature are threaded
    into each backend where the underlying API supports them; subprocess
    backends use ``executable`` overrides when provided.
    """
    import os

    a = config.conducting
    match a.backend:
        case "anthropic_api":
            from coordinare.services.claude import ClaudeService
            api_key_env = a.api_key_env or "ANTHROPIC_API_KEY"
            claude = ClaudeService(
                api_key=os.getenv(api_key_env),
                model=a.model or "claude-sonnet-4-20250514",
                max_tokens=a.max_tokens,
                temperature=a.temperature,
                circuit_breaker=circuit_breaker,
                retry_kwargs=retry_kwargs,
                base_url=a.base_url,
            )
            return AnthropicApiBackend(claude)
        case "openai_api":
            api_key_env = a.api_key_env or "OPENAI_API_KEY"
            return OpenAiApiBackend(
                api_key=os.getenv(api_key_env),
                model=a.model or "gpt-4o-mini",
                max_tokens=a.max_tokens,
                temperature=a.temperature,
                base_url=a.base_url,
                effort=a.effort,
            )
        case "claude_cli":
            return ClaudeCliBackend(executable=a.executable or "claude")
        case "codex_cli":
            return CodexCliBackend(
                executable=a.executable or "codex",
                model=a.model,
                effort=a.effort,
            )
        case "opencode":
            return OpenCodeBackend(
                executable=a.executable or "opencode",
                effort=a.effort,
            )
        case "none":
            return NullBackend()
        case _:
            msg = f"Unknown conducting backend: {a.backend!r}"
            raise ValueError(msg)
