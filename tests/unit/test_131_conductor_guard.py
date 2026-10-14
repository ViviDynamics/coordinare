"""#131 guard: a model call the conductor makes must go through the transcript wrapper.

The rule is enforced on the AST of every module under ``src/coordinare``:

* The model clients (the Anthropic SDK's ``messages.create``, the Anthropic client
  constructor, the ``/chat/completions`` URL, and the model CLIs' subprocess) may appear only
  in the functions that send through :meth:`ConductorCallLog.invoke`.
* Those send functions (``_exchange``, ``_post_chat``, ``_send``) may be called only inside a
  lambda passed to ``invoke``, so every send is recorded.

``bench/`` and ``eval/`` drive their own model calls from outside the daemon. They are not
conductor calls, and they are the only exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "coordinare"
HARNESS_PREFIXES = ("bench/", "eval/")

# (module, enclosing function) pairs that may hold each model-client sink.
_ALLOWED_SINKS: dict[str, frozenset[tuple[str, str]]] = {
    "anthropic_messages_create": frozenset({("services/claude.py", "_exchange")}),
    "anthropic_client": frozenset({("services/claude.py", "__init__")}),
    "chat_completions_url": frozenset({("services/conducting.py", "_post_chat")}),
    "model_cli_subprocess": frozenset({("services/conducting.py", "_run_cli")}),
}
_SEND_FUNCTIONS = frozenset({"_exchange", "_post_chat", "_send"})
# The send functions exist only in these two modules, so other modules' helpers with the same
# names are not checked.
_SEND_MODULES = frozenset({"services/claude.py", "services/conducting.py"})
_CLI_MODULE = "services/conducting.py"


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


def find_violations(source: str, rel: str) -> list[str]:
    """Each direct model call or unwrapped send in ``source``, as ``rel:line: rule``. Harness files are exempt."""
    if rel.startswith(HARNESS_PREFIXES):
        return []
    tree = ast.parse(source)
    parents = _parents(tree)
    docstrings = _docstrings(tree)
    problems: list[str] = []

    def flag(node: ast.AST, rule: str) -> None:
        problems.append(f"{rel}:{getattr(node, 'lineno', 0)}: {rule}")

    def allowed(rule: str, node: ast.AST) -> bool:
        return any((rel, name) in _ALLOWED_SINKS[rule] for name in _enclosing_functions(node, parents))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            func = node.func
            if name == "create" and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute) \
                    and func.value.attr == "messages":
                if not allowed("anthropic_messages_create", node):
                    flag(node, "direct Anthropic messages.create outside the conductor wrapper")
            elif name == "AsyncAnthropic" and not allowed("anthropic_client", node):
                flag(node, "Anthropic client built outside the conductor wrapper")
            elif name == "create_subprocess_exec" and rel == _CLI_MODULE \
                    and not allowed("model_cli_subprocess", node):
                flag(node, "model CLI spawned outside the conductor wrapper")
            if name in _SEND_FUNCTIONS and rel in _SEND_MODULES and not _inside_lambda(node, parents) \
                    and not (_enclosing_functions(node, parents) & _SEND_FUNCTIONS):
                flag(node, f"send {name}() is not passed to ConductorCallLog.invoke")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and "/chat/completions" in node.value and id(node) not in docstrings:
            if not allowed("chat_completions_url", node):
                flag(node, "chat/completions URL outside the conductor wrapper")
    return sorted(problems)


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


@pytest.mark.parametrize(
    ("source", "rel", "rule"),
    [
        (
            "async def ask(client):\n    return await client.messages.create(model='m')\n",
            "services/new_module.py",
            "messages.create",
        ),
        (
            "from anthropic import AsyncAnthropic\nclient = AsyncAnthropic(api_key='k')\n",
            "services/new_module.py",
            "Anthropic client",
        ),
        (
            "async def ask(base):\n    return await post(f'{base}/chat/completions', json={})\n",
            "services/new_module.py",
            "chat/completions",
        ),
        (
            "import asyncio\nasync def ask(p):\n"
            "    return await asyncio.create_subprocess_exec('claude', '--print')\n",
            "services/conducting.py",
            "model CLI",
        ),
        (
            "class B:\n    async def prompt(self, t):\n        return await self._post_chat(t, True)\n",
            "services/conducting.py",
            "not passed to ConductorCallLog.invoke",
        ),
    ],
)
def test_the_guard_flags_a_direct_model_call(source: str, rel: str, rule: str) -> None:
    problems = find_violations(source, rel)
    assert problems, "the guard should have flagged this"
    assert any(rule in problem for problem in problems), problems


def test_the_guard_passes_a_send_wrapped_in_invoke() -> None:
    source = (
        "class B:\n"
        "    async def prompt(self, t):\n"
        "        return await self.calls.invoke(req, lambda: self._post_chat(t, True))\n"
    )
    assert find_violations(source, "services/conducting.py") == []


def test_a_docstring_that_names_the_url_is_not_a_call() -> None:
    source = 'def f():\n    """Posts to /chat/completions. See docs."""\n    return 1\n'
    assert find_violations(source, "services/new_module.py") == []


def test_the_harness_modules_are_exempt() -> None:
    source = "async def judge(c):\n    return await c.post(f'{base}/chat/completions')\n"
    assert find_violations(source, "bench/judge.py") == []
    assert find_violations(source, "eval/gateway.py") == []
    assert find_violations(source, "services/new_module.py") != []
