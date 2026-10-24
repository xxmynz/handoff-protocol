# -*- coding: utf-8 -*-
"""分级交接安全网 / impact-tiered handoff continuity guard.

读 hook JSON（stdin），按 `.agent-workspace.json` 的 writePolicy 分类被改动的路径，并在
回合结束时校验交接连续性。只分析 hook 元数据、文件路径及小型交接文本；不读取 raw/ 等原始数据。
交接文本核验仅使用临时 JSON，不新增内容哈希或完整性框架。

Reads hook JSON on stdin, classifies changed paths via the writePolicy in
`.agent-workspace.json`, and validates handoff continuity at turn end. Hook metadata, file paths
and small handoff texts are analysed; raw data is never read. The observation uses temporary
JSON text, not a new content-hash or integrity framework.
"""

from __future__ import annotations

import hashlib
import ast
import json
import shlex
import re
import sys
import secrets
import time
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol  # noqa: E402  (local sibling module)

DASHBOARD_LIMIT_FALLBACK = 5 * 1024

WRITE_COMMAND_RE = re.compile(
    r"\b(?:Set-Content|Out-File|Add-Content|Copy-Item|Move-Item|New-Item|"
    r"Remove-Item|git\s+apply|sed\s+-i|cp\s|mv\s|touch\s|tee\s)"
    # shell 重定向也是写入：> file / >> file（排除 2>&1、>=、-> 一类）。
    # A shell redirect writes a file too; `2>&1`, `>=` and `->` are not writes.
    r"|(?<![0-9\w])>>?\s*(?!&|=)\S",
    re.IGNORECASE,
)

# DSH / Claude Code 系原生文件工具：参数里直接带 file_path，不从命令行解析。
# Native DSH / Claude Code file tools: the path arrives as an argument.
FILE_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
FILE_TOOL_PATH_KEYS = ("file_path", "filePath", "path", "file")
SHELL_TOOLS = {"bash", "exec_command", "shell", "powershell"}
# 只信任响应开头的错误措辞 / only the response's opening words may name a failure.
ERROR_HEAD_RE = re.compile(r"^\W{0,3}(?:error|failed|failure|exception)\b")
# 桥把 Stop 的 stop_hook_active 固定为 false（DSH 0.2.0-rc.2），该防死循环标记失效；
# 同一个会话连续拦截超过上限后降级为提醒，绝不让 Stop 永久卡住宿主。
# The bridge hardcodes stop_hook_active:false, so the guard counts its own blocks.
STOP_BLOCK_LIMIT = 3


def temp_root(name: str, root: Path | None = None) -> Path:
    """运行时状态目录 / runtime state directory, isolated per project.

    同名的两个克隆不得共享状态，所以项目根也参与目录名 / a clone with the same
    `.agent-workspace.json` name must not share state, so the root hashes in.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name or "workspace")
    if root is not None:
        digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}-{digest}"
    return Path(tempfile.gettempdir()) / f"{safe}-handoff"


def state_path(root: Path, session_id: str) -> Path:
    """按会话隔离状态 / isolate runtime state by session."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return root / f"{safe_id}.json"


def read_payload() -> dict[str, Any]:
    """读取 hook JSON / read hook JSON from stdin."""
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(root: Path, path: Path, state: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)  # 临时状态含小型交接文本，只让当前系统用户访问。
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def tool_text(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input", {})
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "patch"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(tool_input, ensure_ascii=False)


def file_tool_paths(payload: dict[str, Any]) -> list[str]:
    """读取原生文件工具的目标路径 / target paths of native file tools."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    for key in FILE_TOOL_PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return [value]
    return []


def is_successful_tool(payload: dict[str, Any]) -> bool:
    """失败或只读的工具不得触发守卫 / failed tools must never trigger the guard."""
    response = payload.get("tool_response")
    if isinstance(response, dict) and response.get("isError") is True:
        return False
    text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
    head = text.strip()[:200].lower()
    if ERROR_HEAD_RE.match(head):
        return False
    # DSH 桥把 tool_response 压成纯文本，原生文件工具的响应就是**被写入的正文/其 diff**：
    # 正文里出现 "Traceback" 是用户内容，不是失败。崩溃标记因此只用于 shell 工具。
    # The DSH bridge flattens tool_response to text, and a native file tool's
    # response IS the written content or its diff — a "Traceback" inside it is the
    # payload, not a failure. Crash markers therefore stay shell-only.
    if str(payload.get("tool_name", "")).lower() in SHELL_TOOLS:
        codes = re.findall(r"\bexit[ _-]?code\b[\"']?\s*[:=]?\s*(-?\d+)", text, re.IGNORECASE)
        return not (any(int(code) != 0 for code in codes) or "traceback" in head)
    return True


def patched_paths(text: str) -> list[str]:
    return re.findall(r"\*\*\*\s+(?:Update|Add|Delete) File:\s+(.+)", text)


# 候选路径正则的字符类含空格，所以 "tee problems/x.md" 会被整条当成路径；
# 已知命令词不是路径，剥掉它再分类 / the candidate regex allows spaces, so a known
# command word in front of a real path must be stripped before classification.
SHELL_WRITE_VERBS = frozenset({
    "tee", "cp", "mv", "touch", "sed", "dd", "install", "rsync", "rm", "rmdir",
    "mkdir", "cat", "echo", "printf", "git", "patch", "truncate", "sudo",
    "set-content", "out-file", "add-content", "copy-item", "move-item", "new-item",
    "remove-item",
})


def shell_paths(text: str) -> list[str]:
    """从明显写入命令中收集保守的路径线索 / conservative path hints."""
    candidates = re.findall(
        r"(?:[A-Za-z]:[\\/][^\s\"']+|(?:\.{0,2}[\\/])?[\w. -]+[\\/][\w./\\ -]+)",
        text,
    )
    paths = []
    for item in candidates:
        item = item.strip()
        head, sep, tail = item.partition(" ")
        if sep and head.lower() in SHELL_WRITE_VERBS and ("/" in tail or "\\" in tail):
            item = tail.strip()
        paths.append(item)
    return paths


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def classify_write(proto: protocol.Protocol, payload: dict[str, Any]) -> dict[str, list[str]]:
    """分类明显写入；只读或失败工具返回空集合。"""
    result = {"material": [], "ordinary": [], "advisory": [], "dashboard": [], "detail": []}
    if not is_successful_tool(payload):
        return result

    tool_name = str(payload.get("tool_name", ""))
    text = tool_text(payload)
    if tool_name == "apply_patch":
        paths = patched_paths(text)
    elif tool_name.lower() in FILE_TOOLS:
        paths = file_tool_paths(payload)
        if not paths:
            # 路径缺失时不得静默放行 / never pass silently when the path is missing.
            result["ordinary"].append("unresolved file-tool write")
            return result
    elif tool_name.lower() in SHELL_TOOLS:
        if not WRITE_COMMAND_RE.search(text):
            return result
        paths = shell_paths(text)
        if not paths:
            result["ordinary"].append("unresolved local write")
            return result
    else:
        return result

    for path in paths:
        kind = proto.classify(path)
        if kind in result:
            result[kind].append(proto.normalise(path))
    return result


def handoff_contents(proto: protocol.Protocol) -> dict[str, str]:
    """只读小型交接文本，不扫描成果/原始数据，也不引入内容哈希。"""
    result = {}
    root = proto.root.resolve()
    for item in [proto.path(proto.dashboard), *proto.path(proto.handoff_active).glob("*.md")]:
        try:
            resolved = item.resolve()
            if resolved.is_relative_to(root) and resolved.is_file() and resolved.stat().st_size <= 64 * 1024:
                result[item.relative_to(proto.root).as_posix().lower()] = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
    return result


def start_handoff_observation(proto: protocol.Protocol, payload: dict[str, Any]) -> None:
    """启动时留基线；重复启动不得清除已经记录的待交接改动。"""
    session_id = payload.get("session_id")
    if not session_id:
        return
    root = temp_root(proto.name, proto.root)
    path = state_path(root, str(session_id))
    state = load_state(path)
    if "handoff_observed" not in state:
        state["handoff_observed"] = handoff_contents(proto)
        save_state(root, path, state)


def python_write_paths(text: str) -> list[str] | None:
    """只识别明确的内联 Python 目标；静态读语法，不执行/导入用户代码。"""
    if not re.search(r"\bpython(?:3(?:\.\d+)?)?\b", text):
        return None
    match = re.search(r"\bpython(?:3(?:\.\d+)?)?[^\n]*<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)^\1\s*$", text, re.MULTILINE | re.DOTALL)
    try:
        if match:
            code = match.group(2)
        else:
            words = shlex.split(text)
            code = words[words.index("-c") + 1]
        tree = ast.parse(code)
    except (ValueError, IndexError, SyntaxError):
        return []
    variables: dict[str, str] = {}
    paths: list[str] = []

    def value(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return variables.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = value(node.left), value(node.right)
            return str(Path(left) / right) if left is not None and right is not None else None
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("Path", "open") and node.args:
                return value(node.args[0])
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("resolve", "absolute"):
                return value(node.func.value)
        return None

    straight = (ast.Assign, ast.AnnAssign, ast.Expr, ast.With)

    def calls(node: ast.AST):
        if isinstance(node, ast.stmt) and not isinstance(node, straight):
            return
        # 不把未调用函数、条件分支、延迟生成器里的写法当成执行证据。
        if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.If, ast.For, ast.While, ast.Try, ast.IfExp, ast.BoolOp,
                             ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return
        if isinstance(node, ast.Call):
            yield node
        for child in ast.iter_child_nodes(node):
            yield from calls(child)

    # 仅识别直线执行语句/with；不猜函数、分支、循环或任意脚本的运行结果。
    for statement in tree.body:
        if not isinstance(statement, straight):
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                variables.clear()  # 未解释的控制流可能重新绑定先前的路径变量。
            continue
        if isinstance(statement, ast.With) and any(isinstance(node, ast.stmt) and not isinstance(node, straight) for node in ast.walk(statement)):
            variables.clear()
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    resolved = value(statement.value) if statement.value is not None else None
                    if resolved is None:
                        variables.pop(target.id, None)
                    else:
                        variables[target.id] = resolved
        if isinstance(statement, ast.With):
            for item in statement.items:
                if isinstance(item.optional_vars, ast.Name):
                    target = value(item.context_expr)
                    if target is not None:
                        variables[item.optional_vars.id] = target
        for node in calls(statement):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("write_text", "write_bytes", "write", "writelines"):
                target = value(node.func.value)
            elif isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
                mode = node.args[1] if len(node.args) > 1 else next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
                if mode is not None and any(flag in (value(mode) or "") for flag in "wx"):
                    target = value(node.args[0])
            if target is not None:
                paths.append(target)
    return paths


def observed_shell_handoffs(proto: protocol.Protocol, payload: dict[str, Any], state: dict[str, Any], current: dict[str, str]) -> list[str]:
    """终端写入须同时有写入意图、目标路径和真实内容变化；只读/touch 不算。"""
    previous = state.get("handoff_observed")
    if not isinstance(previous, dict) or str(payload.get("tool_name", "")).lower() not in SHELL_TOOLS or not is_successful_tool(payload):
        return []
    text = tool_text(payload)
    python_paths = python_write_paths(text)
    if python_paths is not None:
        targets = {proto.normalise(item) for item in python_paths}
    else:
        if not WRITE_COMMAND_RE.search(text):
            return []
        try:
            # 明显 shell 写入仍受实际内容差异约束；兼容裸 dashboard 文件名。
            targets = {proto.normalise(item) for item in [*shell_paths(text), *shlex.split(text)]}
        except ValueError:
            return []
    return [relative for relative, content in current.items()
            if relative in targets and previous.get(relative) != content]


def handle_post_tool(proto: protocol.Protocol, root: Path, payload: dict[str, Any], path: Path) -> None:
    classified = classify_write(proto, payload)
    state = load_state(path)
    current = handoff_contents(proto)
    # shell 路径猜测不能充当交接凭证（例如 touch 两份文件）；必须有内容差异。
    if str(payload.get("tool_name", "")).lower() in SHELL_TOOLS:
        classified["dashboard"] = []
        classified["detail"] = []
    # 同条命令还改了重要成果时，保守要求随后交接，不猜命令内部的先后次序。
    if not classified["material"]:
        for relative in observed_shell_handoffs(proto, payload, state, current):
            kind = proto.classify(relative)
            if kind in ("dashboard", "detail"):
                classified[kind] = _unique([*classified[kind], relative])
    state["handoff_observed"] = current
    state["workspace"] = str(proto.root.resolve())
    if classified["material"]:
        previous_paths = state.get("material_paths", []) if state.get("material_pending") else []
        state["material_token"] = secrets.token_hex(16)
        state["material_revision_at"] = time.time_ns()
        state["detail_paths"] = []
        state["material_pending"] = True
        state["dashboard_updated"] = False
        state["detail_updated"] = False
        state["material_paths"] = _unique([*previous_paths, *classified["material"]])
    if classified["ordinary"]:
        state["ordinary_pending"] = True
        state["ordinary_paths"] = _unique(classified["ordinary"])[-12:]
    if classified["advisory"]:
        # advisory（例如 .zcode/plans/）只提醒、不阻塞、不要求交接。
        state["advisory_pending"] = True
        state["advisory_paths"] = _unique(classified["advisory"])[-12:]
    if classified["dashboard"]:
        state["dashboard_updated"] = True
        state["dashboard_updated_at"] = time.time_ns()
    if classified["detail"]:
        state["detail_updated"] = True
        state["detail_paths"] = _unique([*state.get("detail_paths", []), *classified["detail"]])
        state["detail_updated_at"] = {**state.get("detail_updated_at", {}), **{item: time.time_ns() for item in classified["detail"]}}

    if state.get("dashboard_updated") and state.get("detail_updated"):
        state["material_pending"] = False
        state["ordinary_pending"] = False
        state["advisory_pending"] = False
    save_state(root, path, state)


def dashboard_issues(proto: protocol.Protocol) -> list[str]:
    """验证 dashboard 大小、链接与已完成的 active handoff。"""
    dashboard = proto.path(proto.dashboard)
    issues: list[str] = []
    if not dashboard.exists():
        return [f"{proto.dashboard} is missing"]
    if dashboard.stat().st_size > proto.dashboard_limit_bytes:
        issues.append(
            f"{proto.dashboard} is {dashboard.stat().st_size} bytes; "
            f"limit is {proto.dashboard_limit_bytes}"
        )

    text = dashboard.read_text(encoding="utf-8")
    links = re.findall(rf"`({re.escape(proto.handoff_active)}[^`]+\.md)`", text)
    links += re.findall(rf"`({re.escape(proto.handoff_archive)}[^`]+\.md)`", text)
    for link in links:
        if link.startswith(proto.handoff_archive):
            issues.append(f"archived handoff remains on dashboard: {link}")
        if not proto.path(link).exists():
            issues.append(f"dashboard link does not resolve: {link}")

    dashboard_links = {proto.normalise(link) for link in links}
    active_root = proto.path(proto.handoff_active)
    completed_re = re.compile(
        r"^Status:\s*(?:Complete(?:d)?\b|Archived\b|Implemented\b|"
        r"Created and verified\b|Adjusted and verified\b)",
        re.IGNORECASE | re.MULTILINE,
    )
    if active_root.exists():
        for handoff in active_root.glob("*.md"):
            content = handoff.read_text(encoding="utf-8")
            relative = handoff.relative_to(proto.root).as_posix().lower()
            if completed_re.search(content):
                issues.append(f"completed workstream still in active/: {relative}")
            elif relative in dashboard_links:
                continue
    return issues


def stop_key(proto: protocol.Protocol, state: dict[str, Any], issues: list[str]) -> str:
    """只用于重复通知去重，不作为交接通过证据。"""
    files = {proto.path(proto.dashboard), *proto.path(proto.handoff_active).glob("*.md")}
    for relative in state.get("material_paths", []):
        item = proto.path(relative).resolve()
        if item.is_relative_to(proto.root.resolve()):
            files.add(item)
    stamps = []
    for item in sorted(files):
        try:
            info = item.stat()
            stamps.append((str(item), info.st_mtime_ns, info.st_size))
        except OSError:
            stamps.append((str(item), None, None))
    # ponytail: 元数据相同的内容替换可能不被识别；去重不授权通过，需要时改为限定文件内容摘要。
    return json.dumps([issues, state.get("material_pending"), state.get("dashboard_updated"),
                       state.get("detail_updated"), stamps], ensure_ascii=False)


def handle_stop(proto: protocol.Protocol, path: Path, payload: dict[str, Any]) -> None:
    state = load_state(path)
    issues = dashboard_issues(proto)
    if state.get("material_pending") and not issues:
        from protocol_handoff_confirm import confirmed
        if confirmed(proto, path, state):
            state["material_pending"] = False
            state["confirmed_token"] = state["material_token"]
            save_state(path.parent, path, state)
    if state.get("material_pending"):
        issues.append(
            "material output or governance files changed without both a detailed handoff "
            f"update and a {proto.dashboard} update; pending session={path.stem} token={state.get('material_token', 'legacy')}"
        )

    if issues:
        key = stop_key(proto, state, issues)
        blocks = int(state.get("stop_blocks", 0))
        retry = (payload.get("stop_hook_active") is not True
                 and state.get("stop_seen") != key and blocks < STOP_BLOCK_LIMIT)
        state["stop_seen"] = key
        message = "Handoff guard: " + "; ".join(issues)
        if retry:
            state["stop_blocks"] = blocks + 1
            print(json.dumps({"decision": "block", "reason": message}, ensure_ascii=False))
        elif state.get("stop_warned") != key:
            state["stop_warned"] = key
            message += "; automatic retries stopped; handoff is still unresolved"
            print(json.dumps({"systemMessage": message}, ensure_ascii=False))
            # DSH 的 Stop 适配器不注入 systemMessage；stderr 会进入 hook/result 的诊断摘要。
            print(message, file=sys.stderr)
        save_state(path.parent, path, state)
        return

    if any(name in state for name in ("stop_blocks", "stop_seen", "stop_warned")):
        for name in ("stop_blocks", "stop_seen", "stop_warned"):
            state.pop(name, None)
        save_state(path.parent, path, state)

    if state.get("ordinary_pending") or state.get("advisory_pending"):
        print(
            json.dumps(
                {
                    "systemMessage": (
                        "Handoff reminder only: ordinary or low-impact files changed. "
                        "Update the handoff only if the change created a reusable result."
                    )
                },
                ensure_ascii=False,
            )
        )


def main() -> int:
    try:
        proto = protocol.load()
    except protocol.ConfigError as error:
        print(f"Handoff guard: {error}", file=sys.stderr)
        return 0  # 配置问题不得阻塞宿主 / a config problem must never block the host.

    root = temp_root(proto.name, proto.root)
    payload = read_payload()
    path = state_path(root, str(payload.get("session_id") or "unknown"))
    event = payload.get("hook_event_name")
    if event == "PostToolUse":
        handle_post_tool(proto, root, payload, path)
    elif event == "Stop":
        handle_stop(proto, path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
