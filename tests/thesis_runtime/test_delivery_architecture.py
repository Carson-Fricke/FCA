"""Static Option 1 boundaries; these checks never import or execute Thesis code.

Mutation cases exercise the guards, not runtime behavior. Delivery/recovery
behavior remains the responsibility of the separate integration tests.
Instructions cannot guarantee obedience, honesty, or loader support; these
selected structural checks do not certify the full behavioral contract.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
RUNTIME = ROOT / "src/thesis_runtime"
NATIVE = {
    "ProviderMessage",
    "ChatMessage",
    "ProviderChatMessage",
    "ToolCall",
    "normalize_tool_history",
}
ROLES = {"user", "assistant", "system", "tool"}
REQUIRED = (
    "src/thesis_runtime/contracts/messages.py",
    "src/thesis_runtime/contracts/compaction.py",
    "src/thesis_runtime/contracts/completion.py",
    "src/thesis_runtime/sessions/compaction.py",
    "src/thesis_runtime/sessions/context_manager.py",
    "src/thesis_runtime/conversations/inbox.py",
    "src/thesis_runtime/sessions/session.py",
    "src/thesis_runtime/sessions/model_invocation.py",
    "src/thesis_runtime/sessions/recovery.py",
    "src/thesis_runtime/contracts/output.py",
    "src/thesis_runtime/conversations/timeline.py",
    "src/thesis_runtime/conversations/incoming.py",
    "src/thesis_runtime/conversations/conversation.py",
    "src/thesis_runtime/conversations/handle.py",
    "src/thesis_runtime/conversations/execution.py",
    "src/thesis_runtime/conversations/output.py",
    "src/thesis_runtime/conversations/recovery.py",
    "src/thesis_runtime/conversations/delivery.py",
    "src/thesis_runtime/adapters/providers/session_adapter.py",
    "src/thesis_runtime/adapters/providers/families/__init__.py",
    "src/thesis_runtime/adapters/providers/families/base.py",
    "src/thesis_runtime/adapters/providers/families/deepseek.py",
)


@lru_cache(maxsize=32)
def _parsed(path: str, source: str) -> ast.Module:
    return ast.parse(source, filename=path)


def _source(path: str) -> ast.Module:
    # Every access still checks existence and reads current bytes. Only parsing
    # identical content is shared; deleted or changed modules cannot hit a bypass.
    file = ROOT / path
    assert file.is_file(), f"Required Option 1 module is missing: {path}"
    return _parsed(path, file.read_text(encoding="utf-8"))


def test_source_cache_observes_changed_and_missing_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    source = tmp_path / "module.py"
    source.write_text("value = 1\n", encoding="utf-8", newline="\n")
    before = ast.dump(_source("module.py"))
    source.write_text("value = 2\n", encoding="utf-8", newline="\n")
    assert ast.dump(_source("module.py")) != before
    source.unlink()
    with pytest.raises(AssertionError, match="module is missing"):
        _source("module.py")


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


@lru_cache(maxsize=32)
def _aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
        elif isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = (
                    item.name if item.asname else item.name.split(".")[0]
                )
    # Resolve ordinary rebinding as well as import aliases and module aliases.
    assignments = [
        node for node in ast.walk(tree) if isinstance(node, ast.Assign | ast.AnnAssign)
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value = _resolve(_name(node.value), aliases)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                key = target.id if isinstance(target, ast.Name) else ""
                if (
                    key
                    and value
                    and key not in aliases
                    and key != value
                    and not value.startswith(key + ".")
                ):
                    aliases[key] = value
                    changed = True
        if not changed:
            break
    return aliases


def _resolve(name: str, aliases: dict[str, str]) -> str:
    seen: set[str] = set()
    for _ in range(min(len(aliases) + 1, 16)):
        if not name or name in seen:
            break
        seen.add(name)
        head, dot, tail = name.partition(".")
        replacement = aliases.get(name, aliases.get(head, head) + dot + tail)
        if replacement == name:
            break
        name = replacement
    return name


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    assert len(found) == 1, f"Expected exactly one {name} class"
    return found[0]


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == name
    ]
    assert len(found) == 1, f"Expected exactly one {name} function"
    return found[0]


def _fields(tree: ast.ClassDef) -> dict[str, str]:
    return {
        node.target.id: ast.unparse(node.annotation)
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def _incoming_create_errors(tree: ast.Module, path: str) -> list[str]:
    """Allow only the canonical writer and explicitly bounded output writes."""
    # Resolving aliases can only reach create_message if that identifier occurs
    # in an attribute, name or imported alias. Scan every module, including ones
    # without calls; avoid building alias/parent maps for unrelated modules.
    if not any(
        isinstance(node, ast.Attribute)
        and node.attr == "create_message"
        or isinstance(node, ast.Name)
        and node.id == "create_message"
        or isinstance(node, ast.alias)
        and node.name.rsplit(".", 1)[-1] == "create_message"
        for node in ast.walk(tree)
    ):
        return []
    aliases = _aliases(tree)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    errors: list[str] = []
    for call in _calls(tree):
        if _resolve(_name(call.func), aliases).rsplit(".", 1)[-1] != "create_message":
            continue
        owner: ast.AST = call
        while owner in parents and not isinstance(
            owner, ast.FunctionDef | ast.AsyncFunctionDef
        ):
            owner = parents[owner]
        owner_name = (
            owner.name
            if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef)
            else ""
        )
        if (
            path == "src/thesis_runtime/conversations/incoming.py"
            and owner_name == "write_incoming"
        ):
            continue
        if (
            path == "src/thesis_runtime/conversations/output.py"
            and owner_name == "persist_turn"
        ):
            # Its outgoing-only rejection is checked separately, including telemetry starts.
            continue
        if path == "src/thesis_runtime/conversations/service.py" and any(
            keyword.arg == "role"
            and _resolve(_name(keyword.value), aliases).endswith(
                "MessageRole.ASSISTANT"
            )
            for keyword in call.keywords
        ):
            continue
        if path == "src/thesis_runtime/conversations/service.py":
            ancestor: ast.AST = call
            assistant_only = False
            while ancestor in parents:
                child = ancestor
                ancestor = parents[ancestor]
                if isinstance(ancestor, ast.If) and child in ancestor.body:
                    tests = (
                        ancestor.test.values
                        if isinstance(ancestor.test, ast.BoolOp)
                        and isinstance(ancestor.test.op, ast.And)
                        else [ancestor.test]
                    )
                    assistant_only = any(
                        isinstance(test, ast.Compare)
                        and _name(test.left) == "role"
                        and any(isinstance(op, ast.Is | ast.Eq) for op in test.ops)
                        and any(
                            _name(value).endswith("MessageRole.ASSISTANT")
                            for value in test.comparators
                        )
                        for test in tests
                    )
                    if assistant_only:
                        break
            if assistant_only:
                continue
        errors.append(
            f"{path}:{call.lineno}: {owner_name} bypasses incoming.write_incoming"
        )
    return errors


def _request_order_errors(run: ast.AST) -> list[str]:
    snapshots = [
        call
        for call in _calls(run)
        if _name(call.func) == "self._provider.request_snapshot"
    ]
    starts = [
        call
        for call in _calls(run)
        if _name(call.func) == "self._record_invocation"
        and any(
            keyword.arg == "kind"
            and _name(keyword.value) == "AgentInvocationKind.MODEL"
            for keyword in call.keywords
        )
        and any(
            keyword.arg == "state"
            and _name(keyword.value) == "AgentInvocationState.STARTED"
            for keyword in call.keywords
        )
    ]
    completes = [
        call for call in _calls(run) if _name(call.func) == "self._provider.stream"
    ]
    if len(snapshots) != 1 or len(starts) != 1 or len(completes) != 1:
        return [
            "Model boundary must call request_snapshot, persist STARTED, and complete exactly once"
        ]
    if not snapshots[0].lineno < starts[0].lineno < completes[0].lineno:
        return [
            "Required order: request_snapshot < durable model STARTED < provider.complete"
        ]
    blocks = [
        value
        for node in ast.walk(run)
        for _, value in ast.iter_fields(node)
        if isinstance(value, list) and any(isinstance(item, ast.stmt) for item in value)
    ]
    if not any(
        any(
            isinstance(item, ast.Assign) and item.value is snapshots[0]
            for item in block
        )
        and any(
            isinstance(item, (ast.Assign, ast.Expr)) and item.value is starts[0]
            for item in block
        )
        and any(
            isinstance(item, (ast.Try, ast.Return)) and completes[0] in _calls(item)
            for item in block
        )
        for block in blocks
    ):
        return ["Model STARTED audit must be unconditional before provider execution"]
    fields = {keyword.arg: keyword.value for keyword in starts[0].keywords}
    if any(
        _name(fields.get(field)) != field
        for field in ("request_messages", "provider_request", "input_hash")
    ):
        return [
            "Model STARTED must persist reference-only request_messages and opaque provider_request/input_hash"
        ]
    return []


def _request_reference_errors(run: ast.AST) -> list[str]:
    assignments = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and any(
            _name(target) == "request_messages"
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    ]
    if len(assignments) != 1:
        return ["Request audit must have one reference-only message snapshot"]
    value = assignments[0].value
    if not (
        isinstance(value, ast.Call)
        and _name(value.func) == "tuple"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.GeneratorExp)
    ):
        return [
            "Request audit must retain ordered message references, not message bodies"
        ]
    generator = value.args[0]
    if not (
        len(generator.generators) == 1
        and _name(generator.generators[0].iter) == "working"
        and not generator.generators[0].ifs
        and not generator.generators[0].is_async
        and isinstance(generator.elt, ast.Dict)
    ):
        return ["Request references must cover the exact working snapshot in order"]
    entry = generator.elt
    fields = {
        key.value: item
        for key, item in zip(entry.keys, entry.values, strict=True)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    reference_fields = {"message_id", "direction", "position"}
    variable = _name(generator.generators[0].target)
    if (
        len(entry.keys) != 3
        or fields.keys() != reference_fields
        or not variable
        or any(_name(fields[key]) != f"{variable}.{key}" for key in reference_fields)
    ):
        return [
            "Request audit entries must contain only message_id, direction, position"
        ]
    for call in _calls(run):
        if _name(call.func) != "self._record_invocation" or not any(
            keyword.arg == "kind"
            and _name(keyword.value) == "AgentInvocationKind.MODEL"
            for keyword in call.keywords
        ):
            continue
        fields = {keyword.arg: keyword.value for keyword in call.keywords}
        if any(
            _name(fields.get(key)) != key
            for key in ("request_messages", "provider_request", "input_hash")
        ):
            return [
                "Every model state update must preserve references and opaque audit/hash"
            ]
    return []


def _terminal_audit_errors(method: ast.AST) -> list[str]:
    updates = [call for call in _calls(method) if _name(call.func) == "replace"]
    if (
        len(updates) != 1
        or not updates[0].args
        or _name(updates[0].args[0]) != "self.invocation"
    ):
        return ["Terminal updates must retain the audited invocation"]
    if any(
        keyword.arg
        in {"request_messages", "provider_request", "input_hash", "created_at"}
        for keyword in updates[0].keywords
    ):
        return ["Terminal updates must not replace the immutable request audit"]
    return []


def _neutral_errors(tree: ast.AST) -> list[str]:
    aliases = _aliases(tree)
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "thesis_runtime.adapters.providers"
        ):
            for item in node.names:
                if item.name in NATIVE or item.name == "*":
                    errors.append(f"line {node.lineno}: native import {item.name}")
        if isinstance(node, ast.Call):
            name = _resolve(_name(node.func), aliases)
            if name.rsplit(".", 1)[-1] in NATIVE:
                errors.append(f"line {node.lineno}: native call {name}")
            for keyword in node.keywords:
                if (
                    keyword.arg == "role"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value in ROLES
                ):
                    errors.append(f"line {node.lineno}: native role construction")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "append",
                "extend",
                "put",
                "put_nowait",
            }:
                receiver = _name(node.func.value).lower()
                if receiver.endswith("working") or any(
                    part in receiver
                    for part in ("result_queue", "resultqueue", "provider_history")
                ):
                    errors.append(f"line {node.lineno}: alternate history/queue write")
        if isinstance(node, ast.Dict):
            keys = {
                key.value: value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            role = keys.get("role")
            if (
                isinstance(role, ast.Constant)
                and role.value in ROLES
                and keys.keys() & {"content", "tool_calls", "tool_call_id"}
            ):
                errors.append(f"line {node.lineno}: native history mapping")
    return errors


def _adapter_errors(tree: ast.AST) -> list[str]:
    aliases = _aliases(tree)
    errors: list[str] = []
    allowed = (
        "__future__",
        "json",
        "collections",
        "typing",
        "dataclasses",
        "types",
        "uuid",
        "contextlib",
        "thesis_runtime.contracts.output",
        "thesis_runtime.contracts.completion",
        "thesis_runtime.contracts.messages",
        "thesis_runtime.contracts.records",
        "thesis_runtime.contracts.activity",
        "thesis_runtime.adapters.providers.invocations",
        "thesis_runtime.adapters.providers.families",
    )
    forbidden_calls = {
        "open",
        "__import__",
        "eval",
        "exec",
        "ChatService",
        "getattr",
        "hasattr",
        "vars",
        "dir",
        "ack",
        "acknowledge",
        "acknowledge_input",
        "dispatch",
        "execute",
        "handler",
        "callback",
        "execute_callback",
        "on_turn",
        "on_consumed",
        "write_incoming",
        "deliver_session_message",
        "normalize_tool_history",
    }
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
            if node.level or any(item.name == "*" for item in node.names):
                errors.append(f"line {node.lineno}: opaque adapter import")
            if any(item.name == "ChatService" for item in node.names):
                errors.append(f"line {node.lineno}: ChatService import")
        elif isinstance(node, ast.Import):
            modules = [item.name for item in node.names]
        for module in modules:
            if not any(
                module == item or module.startswith(item + ".") for item in allowed
            ):
                assert isinstance(node, ast.Import | ast.ImportFrom)
                errors.append(f"line {node.lineno}: effectful adapter import {module}")
        if isinstance(node, ast.Attribute) and node.attr in {
            "persistence",
            "storage",
            "_service",
            "_session",
            "_inbox",
            "__dict__",
        }:
            errors.append(f"line {node.lineno}: service introspection {node.attr}")
    for call in _calls(tree):
        name = _resolve(_name(call.func), aliases)
        leaf = name.rsplit(".", 1)[-1]
        if leaf in forbidden_calls or leaf.startswith(
            (
                "persist",
                "_persist",
                "schedule",
                "_schedule",
                "save_",
                "create_message",
                "write_",
                "read_",
            )
        ):
            errors.append(f"line {call.lineno}: adapter effect {name}")
        if leaf == "stream" and name not in {"self._provider.stream", "self.stream"}:
            errors.append(f"line {call.lineno}: non-injected completion {name}")
        if leaf == "complete":
            errors.append(
                f"line {call.lineno}: blocking completion outside the stream {name}"
            )
    projector = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "project_native_messages"
        ),
        None,
    )
    if projector is not None or any(
        isinstance(node, ast.FunctionDef) and node.name == "request_snapshot"
        for node in ast.walk(tree)
    ):
        # Inspect local helper dependencies too: moving I/O into a helper is not purity.
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        pending = [projector] if projector is not None else []
        pending.extend(
            node for node in functions.values() if node.name == "request_snapshot"
        )
        visited: set[str] = set()
        while pending:
            function = pending.pop()
            if function.name in visited:
                continue
            visited.add(function.name)
            for call in _calls(function):
                name = _resolve(_name(call.func), aliases)
                if name.endswith((".complete", ".stream")) or name.startswith("uuid."):
                    errors.append(f"line {call.lineno}: transport/I/O in projector")
                if name in functions:
                    pending.append(functions[name])
    return errors


@pytest.mark.parametrize("path", REQUIRED)
def test_required_option_one_modules_exist(path: str) -> None:
    _source(path)


def test_session_contracts_are_neutral_and_correlation_is_uniform() -> None:
    tree = _source("src/thesis_runtime/contracts/messages.py")
    message = _class(tree, "SessionMessage")
    content = _class(tree, "MessageContent")
    correlation = _class(tree, "ToolCorrelation")
    assert "role" not in _fields(message)
    assert _fields(message)["content"] == "MessageContent"
    assert set(_fields(correlation)) - {"__pydantic_config__"} == {
        "request_message_id",
        "request_id",
        "tool_name",
    }
    assert "ToolCorrelation" in _fields(content)["correlation"]
    for contract in (message, content, correlation):
        assert any(
            isinstance(decorator, ast.Call)
            and _name(decorator.func) == "dataclass"
            and any(
                keyword.arg == "frozen"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in decorator.keywords
            )
            for decorator in contract.decorator_list
        ), f"{contract.name} must be an immutable dataclass"
    assert not _neutral_errors(tree)


def _incoming_delivery_errors(value: ast.Call) -> list[str]:
    """Tool receipts require correlation; runtime evidence must not fabricate it."""
    errors = []
    fields = {keyword.arg: keyword.value for keyword in value.keywords}
    direction = fields.get("direction")
    if not isinstance(direction, ast.Constant) or direction.value != "incoming":
        errors.append("delivery must explicitly be incoming")
    content = fields.get("content")
    if not isinstance(content, ast.Call) or _name(content.func) != "MessageContent":
        return [*errors, "delivery must construct neutral MessageContent"]
    contents = {keyword.arg: keyword.value for keyword in content.keywords}
    source = contents.get("source")
    if not isinstance(source, ast.Constant) or source.value not in ("tool", "runtime"):
        return [*errors, "session delivery source needs an explicit audited producer"]
    correlated = any(_name(call.func) == "ToolCorrelation" for call in _calls(content))
    if source.value == "tool" and not correlated:
        errors.append("tool result requires explicit correlation")
    if source.value == "runtime" and correlated:
        errors.append("runtime evidence must not fabricate tool correlation")
    return errors


@pytest.mark.parametrize(
    ("source", "correlated", "valid"),
    [
        ("tool", True, True),
        ("tool", False, False),
        ("runtime", False, True),
        ("runtime", True, False),
        ("unknown", False, False),
    ],
)
def test_delivery_guard_distinguishes_tool_receipts_from_runtime_evidence(
    source, correlated, valid
):
    correlation = "ToolCorrelation('message', 'call', 'work')" if correlated else "None"
    tree = ast.parse(
        f"SessionMessage(message_id='id', direction='incoming', content=MessageContent(text='', source='{source}', correlation={correlation}))"
    )
    value = next(call for call in _calls(tree) if _name(call.func) == "SessionMessage")
    assert (not _incoming_delivery_errors(value)) is valid


def test_session_has_no_native_history_or_parallel_result_intake() -> None:
    tree = _source("src/thesis_runtime/sessions/session.py")
    context = _source("src/thesis_runtime/sessions/context_manager.py")
    assert not _neutral_errors(context), "\n".join(_neutral_errors(context))
    assert not _neutral_errors(tree), "\n".join(_neutral_errors(tree))
    assert _fields(_class(tree, "SessionTurn"))["message"] == "SessionMessage"
    assert "role" not in _fields(_class(tree, "SessionTurn"))
    context_calls = _calls(_class(context, "SessionCompaction"))
    for call in context_calls:
        if (
            _name(call.func) == "inbox.deliver"
            and call.args
            and isinstance(call.args[0], ast.Call)
        ):
            assert not _incoming_delivery_errors(call.args[0]), (
                _incoming_delivery_errors(call.args[0])
            )
    run = _function(_class(tree, "AgentSession"), "run")
    deliveries = [call for call in _calls(run) if _name(call.func) == "inbox.deliver"]
    results = [
        call
        for call in deliveries
        if call.args
        and isinstance(call.args[0], ast.Call)
        and _name(call.args[0].func) == "SessionMessage"
    ]
    assert results, (
        "Dispatched results must actually enter inbox.deliver(SessionMessage(...))"
    )
    assert any(_name(call.func).endswith(".dispatch") for call in _calls(run))
    for delivery in results:
        value = delivery.args[0]
        assert isinstance(value, ast.Call)
        assert not _incoming_delivery_errors(value), _incoming_delivery_errors(value)
    assert any(_name(call.func) == "SessionInbox" for call in _calls(run))
    assert any(_name(call.func) == "inbox.snapshot" for call in _calls(run))
    assert not any(
        _name(call.func).endswith("normalize_tool_history") for call in _calls(tree)
    )


def test_inbox_persists_before_admitting_and_does_not_ack_on_delivery() -> None:
    tree = _source("src/thesis_runtime/conversations/inbox.py")
    assert not _neutral_errors(tree)
    inbox = _class(tree, "SessionInbox")
    deliver = _function(inbox, "deliver")
    annotation = deliver.args.args[-1].annotation
    assert annotation is not None and ast.unparse(annotation) == "SessionMessage"
    writes = [call for call in _calls(deliver) if _name(call.func) == "self._persist"]
    assert len(writes) == 1, "Inbox delivery must invoke its injected canonical writer"
    admissions = [
        node
        for node in ast.walk(deliver)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and _name(target.value) == "self._messages"
            for target in node.targets
        )
    ]
    assert admissions and all(node.lineno > writes[0].lineno for node in admissions)
    assert not any(
        _name(call.func)
        in {"self.acknowledge", "self._consumed.add", "self._history.append"}
        for call in _calls(deliver)
    )
    pending = _function(inbox, "pending")
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "position"
        for node in ast.walk(inbox)
    )
    assert not any(
        _name(call.func).endswith((".acknowledge", ".difference_update", ".clear"))
        for call in _calls(pending)
    )


def test_adapter_is_pure_and_completion_uses_the_shared_projector() -> None:
    tree = _source("src/thesis_runtime/adapters/providers/session_adapter.py")
    errors = _adapter_errors(tree)
    assert not errors, "\n".join(errors)
    for file in sorted((RUNTIME / "adapters/providers/families").rglob("*.py")):
        path = file.relative_to(ROOT).as_posix()
        errors = _adapter_errors(_source(path))
        assert not errors, f"{path}:\n" + "\n".join(errors)
    _function(tree, "project_native_messages")
    wrapper = _function(_class(tree, "NativeSessionAdapter"), "stream")
    assert (
        wrapper.returns is not None
        and ast.unparse(wrapper.returns)
        == "Generator[OutputDelta | SessionMessage, None, None]"
    ), "Transport completion must return a neutral envelope"
    transport = [
        call for call in _calls(wrapper) if _name(call.func) == "self._provider.stream"
    ]
    assert len(transport) == 1
    preparation = _function(_class(tree, "NativeSessionAdapter"), "request_snapshot")
    assert any(
        _name(call.func) == "project_native_messages" for call in _calls(preparation)
    )
    assert any(_name(call.func) == "self._snapshot" for call in _calls(wrapper))
    assert any(_name(call.func) == "self._snapshot" for call in _calls(preparation))
    assert not any(
        _name(call.func) in {"ProviderMessage", "ChatMessage"}
        for call in _calls(wrapper)
    ), "Execution must not reconstruct messages from the audit snapshot"
    assert any(_name(call.func) == "SessionMessage" for call in _calls(wrapper))
    for path in (
        "src/thesis_runtime/sessions/session.py",
        "src/thesis_runtime/conversations/conversation.py",
        "src/thesis_runtime/conversations/handle.py",
        "src/thesis_runtime/conversations/execution.py",
        "src/thesis_runtime/conversations/output.py",
        "src/thesis_runtime/conversations/recovery.py",
    ):
        assert not _neutral_errors(_source(path)), (
            f"Runtime must not seed/project native history: {path}"
        )


def test_one_incoming_writer_and_outgoing_only_turn_persistence() -> None:
    incoming = _source("src/thesis_runtime/conversations/incoming.py")
    writer = _function(incoming, "write_incoming")
    creates = [
        call
        for call in _calls(incoming)
        if _name(call.func).endswith(".create_message")
    ]
    assert creates and all(call in _calls(writer) for call in creates), (
        "Only write_incoming may create incoming rows"
    )
    conversation = _source("src/thesis_runtime/conversations/conversation.py")
    canonical = _function(
        _class(conversation, "Conversations"), "deliver_session_message"
    )
    aliases = _aliases(conversation)
    assert any(
        _resolve(_name(call.func), aliases).rsplit(".", 1)[-1] == "write_incoming"
        for call in _calls(canonical)
    ), "Conversation composition must reach write_incoming"
    assert _calls(canonical), "An unused/empty canonical method is not routing proof"
    handle = _source("src/thesis_runtime/conversations/handle.py")
    send = _function(_class(handle, "Conversation"), "send")
    assert any(
        _name(call.func).endswith(".deliver_session_message") for call in _calls(send)
    ), "Public operator intake must call canonical incoming delivery"
    output = _function(
        _source("src/thesis_runtime/conversations/output.py"), "persist_turn"
    )
    guards = [
        node
        for node in ast.walk(output)
        if isinstance(node, ast.If)
        and any(
            isinstance(value, ast.Compare)
            and isinstance(value.left, ast.Attribute)
            and value.left.attr == "direction"
            and any(isinstance(op, ast.NotEq) for op in value.ops)
            and any(
                isinstance(right, ast.Constant) and right.value == "outgoing"
                for right in value.comparators
            )
            for value in ast.walk(node.test)
        )
        and any(
            isinstance(value, ast.Return | ast.Raise)
            for statement in node.body
            for value in ast.walk(statement)
        )
    ]
    assert guards, "Outgoing persistence must reject incoming turns before writing"
    output_creates = [
        call for call in _calls(output) if _name(call.func).endswith(".create_message")
    ]
    assert output_creates and all(
        call.lineno > min(guard.lineno for guard in guards) for call in output_creates
    )
    for path in (
        "src/thesis_runtime/conversations/conversation.py",
        "src/thesis_runtime/conversations/delivery.py",
    ):
        tree = conversation if path.endswith("conversation.py") else _source(path)
        extra = [
            call.lineno
            for call in _calls(tree)
            if _name(call.func).endswith(".create_message")
        ]
        assert not extra, f"Independent incoming persistence in {path}: {extra}"


def test_delivery_service_delegates_incoming_persistence() -> None:
    tree = _source("src/thesis_runtime/conversations/delivery.py")
    assert not any(
        _name(call.func).endswith(".create_message") for call in _calls(tree)
    ), "DeliveryService must not own another incoming writer"
    deliver = _function(_class(tree, "DeliveryService"), "deliver")
    aliases = _aliases(tree)
    assert any(
        _resolve(_name(call.func), aliases).endswith(
            ("write_incoming", "deliver_session_message", "_deliver_session_message")
        )
        for call in _calls(deliver)
    ), "Delivery intake must invoke its canonical writer callback"


def test_runtime_incoming_creates_are_not_hidden_in_other_modules() -> None:
    violations: list[str] = []
    for path in RUNTIME.rglob("*.py"):
        if path.name in {"incoming.py", "conversation.py"}:
            continue
        tree = _source(path.relative_to(ROOT).as_posix())
        violations.extend(
            _incoming_create_errors(tree, path.relative_to(ROOT).as_posix())
        )
    assert not violations, f"Alternate runtime incoming writers: {violations}"


def test_all_production_message_creation_uses_the_canonical_input_boundary() -> None:
    _source("src/thesis_runtime/conversations/service.py")
    _source("src/thesis_runtime/conversations/incoming.py")
    violations: list[str] = []
    for path in (
        *((ROOT / "src/thesis").rglob("*.py")),
        *((ROOT / "src/thesis_runtime").rglob("*.py")),
    ):
        relative = path.relative_to(ROOT).as_posix()
        violations.extend(_incoming_create_errors(_source(relative), relative))
    assert not violations, "\n".join(violations)


def test_storage_convenience_intakes_call_the_actual_incoming_writer() -> None:
    tree = _source("src/thesis_runtime/conversations/service.py")
    service = _class(tree, "ChatService")
    aliases = _aliases(tree)
    # Follow local helper calls rather than treating a helper's name as proof.
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for name in ("send_message", "launch_workflow", "post_steering_message"):
        pending = [_function(service, name)]
        visited: set[str] = set()
        reached = False
        while pending:
            function = pending.pop()
            if function.name in visited:
                continue
            visited.add(function.name)
            for call in _calls(function):
                callee = _resolve(_name(call.func), aliases)
                if callee == "thesis_runtime.conversations.incoming.write_incoming":
                    reached = True
                local = callee.removeprefix("self.")
                if local in functions:
                    pending.append(functions[local])
        assert reached, f"ChatService.{name} must reach incoming.write_incoming"


def test_http_message_posts_use_root_send_and_bound_steering_factory() -> None:
    tree = _source("src/thesis/http/chat.py")
    handler = _function(tree, "create_message")
    assert not _incoming_create_errors(tree, "src/thesis/http/chat.py")
    assert any(_name(call.func) == "conversations.root" for call in _calls(handler))
    assert any(_name(call.func) == "root.send" for call in _calls(handler))
    steering = [
        call
        for call in _calls(handler)
        if _name(call.func) == "chat_service.post_steering_message"
    ]
    assert len(steering) == 1
    fields = {keyword.arg: keyword.value for keyword in steering[0].keywords}
    factory = fields.get("message_factory")
    assert isinstance(factory, ast.Call) and _name(factory.func) == "partial"
    assert factory.args and _name(factory.args[0]) == "root.send"
    assert any(
        keyword.arg == "schedule"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in factory.keywords
    )
    assert _name(fields.get("after_delivery")) == "conversations.schedule_input"


def test_model_snapshot_is_durable_before_provider_execution() -> None:
    tree = _source("src/thesis_runtime/sessions/model_invocation.py")
    run = _function(_class(tree, "ModelInvocation"), "execute")
    assert not _request_order_errors(run)
    assert not _request_reference_errors(run)
    recorder = _function(_class(tree, "ModelInvocation"), "_record_invocation")
    records = [call for call in _calls(recorder) if _name(call.func) == "replace"]
    saves = [
        call
        for call in _calls(recorder)
        if _name(call.func) == "self._store.save_invocation"
    ]
    assert len(records) == len(saves) == 1
    assert records[0].lineno < saves[0].lineno
    fields = {keyword.arg: keyword.value for keyword in records[0].keywords}
    for field in ("request_messages", "provider_request", "input_hash"):
        assert _name(fields.get(field)) == field
    assert len(saves[0].args) == 1 and _name(saves[0].args[0]) == "self.invocation"
    for method in ("commit", "interrupt"):
        assert not _terminal_audit_errors(
            _function(_class(tree, "ModelInvocation"), method)
        )
    adapter = _source("src/thesis_runtime/adapters/providers/session_adapter.py")
    snapshot = _function(_class(adapter, "NativeSessionAdapter"), "_snapshot")
    keys = {
        key.value
        for node in ast.walk(snapshot)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert {"messages", "tools", "projection", "role", "response_format"} <= keys
    assert keys & {
        "tool_catalog_version",
        "catalog_version",
        "toolCatalogVersion",
        "catalogVersion",
    }, "Provider audit must retain an explicit tool-catalog version"
    assert not _neutral_errors(tree), (
        "Runtime must treat provider_request as opaque audit data"
    )


@pytest.mark.parametrize(
    "code",
    [
        "def send_message(self):\n    return self.create_message(body='input')",
        "def launch_workflow(self):\n    write = self.create_message\n    return write(body='trigger')",
        "def fallback(self):\n    return self.create_message(role=MessageRole.SYSTEM)",
        "def send_message(self, role, incoming):\n    if role is MessageRole.ASSISTANT or incoming:\n        return self.create_message(role=role)",
    ],
)
def test_production_incoming_guard_rejects_convenience_bypasses(code: str) -> None:
    assert _incoming_create_errors(
        ast.parse(code), "src/thesis_runtime/conversations/service.py"
    )


def test_production_incoming_guard_allows_only_named_boundary_and_assistant_output() -> (
    None
):
    assert not _incoming_create_errors(
        ast.parse(
            "def write_incoming(chat):\n    return chat.create_message(body='input')"
        ),
        "src/thesis_runtime/conversations/incoming.py",
    )
    assert not _incoming_create_errors(
        ast.parse(
            "def serialize(self):\n    return self.create_message(role=MessageRole.ASSISTANT)"
        ),
        "src/thesis_runtime/conversations/service.py",
    )
    assert not _incoming_create_errors(
        ast.parse(
            "def send_message(self, role):\n    if role is MessageRole.ASSISTANT:\n        return self.create_message(role=role)"
        ),
        "src/thesis_runtime/conversations/service.py",
    )


def test_snapshot_guard_rejects_started_before_preparation() -> None:
    tree = ast.parse("""
def run(self):
    self._record_invocation(kind=AgentInvocationKind.MODEL, state=AgentInvocationState.STARTED,
                            request_messages=request_messages, provider_request=provider_request, input_hash=input_hash)
    self._provider.request_snapshot(messages=messages)
    self._provider.stream(messages=messages)
""")
    assert _request_order_errors(_function(tree, "run"))


@pytest.mark.parametrize(
    "expression",
    [
        "tuple(message.as_dict() for message in working)",
        "tuple(working)",
        "tuple({'message_id': message.message_id, 'direction': message.direction, "
        "'position': message.position, 'content': message.content} for message in working)",
        "tuple({'message_id': message.message_id, 'direction': message.direction, "
        "'position': message.position} for message in working if message.position)",
        "tuple({'message_id': message.message_id, 'direction': message.direction, "
        "'position': message.content} for message in working)",
    ],
)
def test_snapshot_guard_rejects_full_or_inexact_message_audits(expression: str) -> None:
    assert _request_reference_errors(ast.parse(f"request_messages = {expression}"))


def test_snapshot_guard_rejects_terminal_audit_loss() -> None:
    tree = _source("src/thesis_runtime/sessions/model_invocation.py")
    terminal = deepcopy(_function(_class(tree, "ModelInvocation"), "commit"))
    update = next(call for call in _calls(terminal) if _name(call.func) == "replace")
    update.keywords.append(
        ast.keyword(arg="provider_request", value=ast.Constant(None))
    )
    assert _terminal_audit_errors(terminal)


def test_snapshot_guard_rejects_conditional_audit_persistence() -> None:
    tree = ast.parse("""
def run(self):
    provider_request = self._provider.request_snapshot(messages=working)
    if audit_enabled:
        invocation = self._record_invocation(kind=AgentInvocationKind.MODEL,
            state=AgentInvocationState.STARTED, request_messages=request_messages,
            provider_request=provider_request, input_hash=input_hash)
    try:
        completion = self._provider.stream(messages=working)
    except ProviderError:
        pass
""")
    assert _request_order_errors(_function(tree, "run"))


def test_required_module_guard_fails_for_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="Required Option 1 module is missing"):
        _source(REQUIRED[0])


def test_root_instructions_load_contract_and_matching_globs() -> None:
    contract = ".github/instructions/agent-session-delivery.instructions.md"
    root = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"@{contract}" in root.splitlines()
    text = (ROOT / contract).read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    for glob in (
        "src/thesis/agent/**",
        "src/thesis/application/**",
        "src/thesis/extensions/**",
        "src/thesis_runtime/**",
        "src/thesis/workflows/**",
        "src/ui/src/lib/**",
        "tests/thesis/**",
        "tests/thesis_runtime/**",
    ):
        assert glob in frontmatter


@pytest.mark.parametrize(
    "code",
    [
        "from thesis_runtime.adapters.providers.invocations import ChatMessage as Evidence\nEvidence(content='x')",
        "import thesis_runtime.adapters.providers.invocations as wire\nFactory = wire.ChatMessage\nFactory(content='x')",
        "from thesis_runtime.adapters.providers.invocations import ToolCall as Request\nRequest('a', 'b', '{}')",
        "from thesis_runtime.adapters.providers.invocations import normalize_tool_history as clean\nclean(history)",
        "working.append(Message(role='tool', content='x'))",
        "history.append({'role': 'assistant', 'content': 'x'})",
        "self._result_queue.put(result)",
    ],
)
def test_neutral_guard_rejects_native_mutations(code: str) -> None:
    assert _neutral_errors(ast.parse(code))


def test_neutral_guard_allows_model_profiles_and_unrelated_tool_payloads() -> None:
    assert not _neutral_errors(
        ast.parse("""
from typing import Literal
role: Literal['primary', 'coding', 'critique', 'research'] = 'primary'
provider.complete(messages=messages, role=role)
MessageContent(text='tool', source='tool', result={'tool': 'check'})
payload = {'role': 'assistant', 'label': 'display only'}
""")
    )


@pytest.mark.parametrize(
    "code",
    [
        "from thesis_runtime.conversations.service import ChatService as Records",
        "import thesis_runtime.conversations.inbox as neutral",
        "from pathlib import Path as Value",
        "from builtins import open as decode\ndecode('file')",
        "save = store.persist\nsave(message)",
        "ack = inbox.acknowledge\nack(messages)",
        "send = tools.dispatch\nsend('check', {})",
        "getattr(provider, 'persistence')",
        "provider._service",
        "execute_callback()",
        "def project_native_messages(messages):\n    return self._provider.stream(messages=messages)",
        "import uuid\ndef helper():\n    return uuid.uuid4()\ndef project_native_messages(messages):\n    return helper()",
        "import uuid\ndef request_snapshot(messages):\n    return uuid.uuid4()",
    ],
)
def test_adapter_guard_rejects_effectful_mutations(code: str) -> None:
    assert _adapter_errors(ast.parse(code))


def test_adapter_guard_allows_decoding_and_wrapper_transport_not_docstring_effects() -> (
    None
):
    assert not _adapter_errors(
        ast.parse('''
"""Never persist, schedule, acknowledge, dispatch or open a file."""
import uuid
from thesis_runtime.contracts.records import ChatMessage
from thesis_runtime.contracts.activity import decode_activity
def project_native_messages(messages):
    return tuple(messages)
class NativeSessionAdapter:
    def complete(self, messages):
        identity = uuid.uuid4()
        return self._provider.stream(messages=project_native_messages(messages))
''')
    )


def test_local_entrypoint_uses_shared_control_service_without_method_overrides() -> (
    None
):
    """Local wiring may choose adapters, never shadow runtime completion authority."""
    tree = _source("scripts/local/thesis_local_api.py")
    aliases = _aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert all(
                _resolve(_name(base), aliases)
                != "thesis_runtime.subagents.control.ControlToolService"
                for base in node.bases
            ), "Local logging must not override the shared control service contract"
    compositions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _resolve(_name(node.func), aliases)
        == "thesis.application.composition.build_serving_stack"
    ]
    assert len(compositions) == 1
    assert all(keyword.arg != "control_factory" for keyword in compositions[0].keywords)
