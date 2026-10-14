"""#131 guard: a model call the conductor makes must go through the transcript wrapper.

The rules are enforced on the AST of every module under ``src/coordinare``:

* A model SDK (``anthropic``, ``openai``, ``litellm``) is imported only where it is wrapped.
* ``httpx`` reaches a model endpoint only in the conducting backend, which wraps every send.
* The model clients (the Anthropic ``messages.create``, the Anthropic client constructor, the
  ``/chat/completions`` URL, and a model CLI's subprocess) appear only in the functions that
  send through :meth:`ConductorCallLog.invoke`. A method alias of ``messages.create`` is not a
  call, so it is flagged where it is taken.
* A model CLI is spawned only by ``_run_cli``, and ``_run_cli`` is called only by a backend's
  ``_send``.
* The send functions (``_exchange``, ``_post_chat``, ``_send``) are called only inside a lambda
  passed to ``invoke``, so every send is recorded.

``bench/`` and ``eval/`` drive their own model calls from outside the daemon. They are not
conductor calls, and they are the only exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "coordinare"
HARNESS_PREFIXES = ("bench/", "eval/")

#: Model SDKs and the only modules allowed to import them.
_SDK_MODULES: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"services/claude.py"}),
    "openai": frozenset(),
    "litellm": frozenset(),
}
#: Strings that name a model endpoint path. A module holding one is a model client.
_MODEL_PATHS = ("/chat/completions", "/v1/messages", "/v1/responses")
#: Modules where httpx may reach a model endpoint. Each send there goes through the wrapper.
_HTTPX_MODEL_MODULES = frozenset({"services/conducting.py"})
#: The model CLIs. A spawn that names one of them is a model call.
_MODEL_CLIS = frozenset({"claude", "opencode", "codex"})
_SPAWN_CALLS = frozenset({
    "create_subprocess_exec", "create_subprocess_shell", "Popen", "run", "call", "check_output",
    "check_call",
})

# (module, enclosing function) pairs that may hold each model-client sink.
_ALLOWED_SINKS: dict[str, frozenset[tuple[str, str]]] = {
    "anthropic_messages_create": frozenset({("services/claude.py", "_exchange")}),
    "anthropic_client": frozenset({("services/claude.py", "__init__")}),
    "model_path_url": frozenset({("services/conducting.py", "_post_chat")}),
    "model_cli_subprocess": frozenset({("services/conducting.py", "_run_cli")}),
}
_SEND_FUNCTIONS = frozenset({"_exchange", "_post_chat", "_send"})
#: The send functions exist only in these two modules, so other modules' helpers with the same
#: names are not checked.
_SEND_MODULES = frozenset({"services/claude.py", "services/conducting.py"})
_CLI_MODULE = "services/conducting.py"
_RUN_CLI = "_run_cli"


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _enclosing_functions(node: ast.AST, parents: dict[int, ast.AST]) -> set[str]:
    """The names of every function the node sits in, nested ones included."""
    names: set[str] = set()
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(current.name)
        current = parents.get(id(current))
    return names


def _inside_lambda(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.Lambda):
            return True
        current = parents.get(id(current))
    return False


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _strings_in_call(call: ast.Call) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(call):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def find_violations(source: str, rel: str) -> list[str]:
    """Each direct model call, alias or unwrapped send in ``source``, as ``rel:line: rule``.

    Harness files are exempt. A module that does not parse is reported, not skipped.
    """
    if rel.startswith(HARNESS_PREFIXES):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{rel}:{exc.lineno or 0}: does not parse"]
    parents = _parents(tree)
    docstrings = _docstrings(tree)
    problems: list[str] = []

    def flag(node: ast.AST, rule: str) -> None:
        problems.append(f"{rel}:{getattr(node, 'lineno', 0)}: {rule}")

    def allowed(rule: str, node: ast.AST) -> bool:
        return any((rel, name) in _ALLOWED_SINKS[rule] for name in _enclosing_functions(node, parents))

    # 1. Model SDK imports, and httpx in a module that names a model endpoint.
    holds_model_path = any(
        isinstance(node, ast.Constant) and isinstance(node.value, str)
        and any(marker in node.value for marker in _MODEL_PATHS) and id(node) not in docstrings
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            continue
        for module in modules:
            root = module.split(".")[0]
            if root in _SDK_MODULES and rel not in _SDK_MODULES[root]:
                flag(node, f"imports {root} outside the conductor wrapper")
            if root == "httpx" and holds_model_path and rel not in _HTTPX_MODEL_MODULES:
                flag(node, "httpx reaches a model endpoint outside the conductor wrapper")

    for node in ast.walk(tree):
        # 2. Model endpoint paths.
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings \
                and any(marker in node.value for marker in _MODEL_PATHS) \
                and not allowed("model_path_url", node):
            flag(node, "model endpoint path outside the conductor wrapper")

        # 3. The Anthropic messages.create, as a call or as an alias that is not called.
        if isinstance(node, ast.Attribute) and node.attr == "create" \
                and isinstance(node.value, ast.Attribute) and node.value.attr == "messages":
            parent = parents.get(id(node))
            if not (isinstance(parent, ast.Call) and parent.func is node):
                flag(node, "messages.create taken as an alias, not called inside the wrapper")
            elif not allowed("anthropic_messages_create", node):
                flag(node, "direct Anthropic messages.create outside the conductor wrapper")

        if isinstance(node, ast.Call):
            name = _callee_name(node)
            # 4. The Anthropic client.
            if name == "AsyncAnthropic" and not allowed("anthropic_client", node):
                flag(node, "Anthropic client built outside the conductor wrapper")
            # 5. A model CLI spawned outside the wrapper. In the conducting module every spawn
            #    must be inside _run_cli; elsewhere a spawn that names a model CLI is flagged.
            if name in _SPAWN_CALLS:
                names_cli = bool(_strings_in_call(node) & _MODEL_CLIS)
                if (rel == _CLI_MODULE or names_cli) \
                        and not (rel == _CLI_MODULE and _RUN_CLI in _enclosing_functions(node, parents)) \
                        and not allowed("model_cli_subprocess", node):
                    flag(node, "model CLI spawned outside the conductor wrapper")
            # 6. _run_cli is called only by a backend's _send.
            if name == _RUN_CLI and "_send" not in _enclosing_functions(node, parents):
                flag(node, "_run_cli called outside the conductor wrapper")
            # 7. The send functions are called only inside a lambda passed to invoke.
            if name in _SEND_FUNCTIONS and rel in _SEND_MODULES and not _inside_lambda(node, parents) \
                    and not (_enclosing_functions(node, parents) & _SEND_FUNCTIONS):
                flag(node, f"send {name}() is not passed to ConductorCallLog.invoke")
    return sorted(set(problems))


def _modules() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        found.append((rel, path.read_text(encoding="utf-8")))
    return found


def test_no_conductor_model_call_bypasses_the_transcript_wrapper() -> None:
    problems: list[str] = []
    for rel, source in _modules():
        problems.extend(find_violations(source, rel))
    assert problems == [], "direct model calls outside the conductor wrapper:\n" + "\n".join(problems)


def test_the_wrapped_sink_functions_exist_where_the_rule_expects_them() -> None:
    for rel, function in {
        ("services/claude.py", "_exchange"),
        ("services/conducting.py", "_post_chat"),
        ("services/conducting.py", "_run_cli"),
    }:
        source = (SRC / rel).read_text(encoding="utf-8")
        assert f"def {function}(" in source, f"{rel} no longer defines {function}"


# Each rule, with a snippet that must be flagged and the rule's words in the flag.
_FLAGGED: list[tuple[str, str, str]] = [
    # A direct messages.create in a module that is not the wrapper.
    ("async def ask(client):\n    return await client.messages.create(model='m')\n",
     "services/new_module.py", "messages.create"),
    # An alias of messages.create, taken and stored, never called here.
    ("async def ask(self):\n    send = self._client.messages.create\n    return send\n",
     "services/claude.py", "alias"),
    # An alias, even inside the wrapper module.
    ("class C:\n    def f(self):\n        self.call = self._client.messages.create\n",
     "services/claude.py", "alias"),
    # The Anthropic SDK imported where it is not wrapped.
    ("from anthropic import AsyncAnthropic\n", "services/new_module.py", "imports anthropic"),
    # The OpenAI SDK, anywhere.
    ("import openai\n", "services/new_module.py", "imports openai"),
    # LiteLLM, anywhere.
    ("from litellm import completion\n", "services/new_module.py", "imports litellm"),
    # httpx against a model endpoint, in a module that is not the conducting backend.
    ("import httpx\nasync def ask(c):\n    return await c.post(f'{base}/v1/messages')\n",
     "services/new_module.py", "httpx reaches a model endpoint"),
    # The Anthropic client built outside the wrapper.
    ("from anthropic import AsyncAnthropic\nclient = AsyncAnthropic(api_key='k')\n",
     "services/new_module.py", "imports anthropic"),
    # A chat/completions URL outside the wrapper.
    ("async def ask(base):\n    return await post(f'{base}/chat/completions', json={})\n",
     "services/new_module.py", "model endpoint path"),
    # A model CLI spawned by a new module.
    ("import asyncio\nasync def ask(p):\n"
     "    return await asyncio.create_subprocess_exec('claude', '--print')\n",
     "services/new_module.py", "model CLI"),
    # A new subprocess in the conducting module, outside _run_cli.
    ("import asyncio\nasync def ask(p):\n"
     "    return await asyncio.create_subprocess_exec('ls', '-l')\n",
     "services/conducting.py", "model CLI"),
    # _run_cli called from a function that is not a backend's _send.
    ("async def ask(p):\n    return await _run_cli('claude', [], p, label='x', limit=1, events='e')\n",
     "services/conducting.py", "_run_cli called outside"),
    # A send that is not passed to invoke.
    ("class B:\n    async def prompt(self, t):\n        return await self._post_chat(t, True)\n",
     "services/conducting.py", "not passed to ConductorCallLog.invoke"),
]


@pytest.mark.parametrize(("source", "rel", "rule"), _FLAGGED)
def test_the_guard_flags_each_direct_model_call_rule(source: str, rel: str, rule: str) -> None:
    problems = find_violations(source, rel)
    assert problems, "the guard should have flagged this"
    assert any(rule in problem for problem in problems), problems


_CLEAN: list[tuple[str, str]] = [
    # A send wrapped in invoke.
    ("class B:\n    async def prompt(self, t):\n"
     "        return await self.calls.invoke(req, lambda: self._post_chat(t, True))\n",
     "services/conducting.py"),
    # A subprocess spawn inside _run_cli, in the conducting module.
    ("import asyncio\nasync def _run_cli(e, a, p):\n"
     "    return await asyncio.create_subprocess_exec(e, *a)\n",
     "services/conducting.py"),
    # A docstring that names the endpoint is prose.
    ('def f():\n    """Posts to /chat/completions. See docs."""\n    return 1\n', "services/new_module.py"),
    # A plain httpx user with no model endpoint is not a model client.
    ("import httpx\nasync def get(c):\n    return await c.get('https://api.github.com/repos')\n",
     "services/new_module.py"),
    # A non-model subprocess (docker) outside the conducting module.
    ("import asyncio\nasync def run():\n"
     "    return await asyncio.create_subprocess_exec('docker', 'ps')\n",
     "services/new_module.py"),
]


@pytest.mark.parametrize(("source", "rel"), _CLEAN)
def test_the_guard_passes_the_wrapped_and_the_non_model_forms(source: str, rel: str) -> None:
    assert find_violations(source, rel) == []


def test_the_harness_modules_are_exempt() -> None:
    source = "import openai\nasync def judge(c):\n    return await c.post(f'{base}/chat/completions')\n"
    assert find_violations(source, "bench/judge.py") == []
    assert find_violations(source, "eval/gateway.py") == []
    assert find_violations(source, "services/new_module.py") != []


def test_a_module_that_does_not_parse_is_reported() -> None:
    assert find_violations("def broken(:\n", "services/new_module.py")
