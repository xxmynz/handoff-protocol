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

# 只有出现在**命令位置**的动词才是写入；正文里提到的 `cp ` 只是文字。
# 旧版对整个命令行做正则搜索，于是 `grep -n "cp " scripts/x.py` 这种只读命令
# 会被判成改了 `scripts/x.py`，交接怎么更新都清不掉，回合永远收不了尾。
# Only a command-position verb writes; a word merely mentioned is not a command.
SHELL_WRITE_COMMANDS = frozenset({
    "set-content", "out-file", "add-content", "copy-item", "move-item",
    "new-item", "remove-item", "cp", "mv", "touch", "tee",
    "rm", "rmdir", "mkdir", "dd", "truncate", "install", "rsync", "patch",
})
# 这些词先占住命令位置，真正的命令在它们后面 / these run the real command.
SHELL_WRAPPER = frozenset({
    "sudo", "env", "command", "exec", "nohup", "time", "nice", "ionice",
    "timeout", "stdbuf", "xargs",
})
# find 用 -exec 重新进入命令位置 / find re-enters command position after -exec.
SHELL_REEXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# 嵌套 shell 的 -c 里还是命令 / a nested shell's -c carries more commands.
SHELL_NESTED = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
SHELL_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
# 重定向也是写入：> file / >> file / 2> file（排除 2>&1、>=、-> 一类）。
# A redirect writes too; `2>&1`, `>=` and `->` are not writes.
REDIRECT_RE = re.compile(r"^\d*>>?(?!&|=)")
# 写进黑洞不算改了文件 / writing into a bit bucket is not a change.
SHELL_SINKS = frozenset({"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "nul", "nul:", "$null"})

# DSH / Claude Code 系原生文件工具：参数里直接带 file_path，不从命令行解析。
# Native DSH / Claude Code file tools: the path arrives as an argument.
FILE_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
FILE_TOOL_PATH_KEYS = ("file_path", "filePath", "path", "file")
SHELL_TOOLS = {"bash", "exec_command", "shell", "powershell"}
# 只信任响应开头的错误措辞 / only the response's opening words may name a failure.
ERROR_HEAD_RE = re.compile(r"^\W{0,3}(?:error|failed|failure|exception)\b")
# 桥把 Stop 的 stop_hook_active 固定为 false（DSH 0.2.0-rc.2），该防死循环标记失效；
# 同一会话的连续未解决区间最多拦一次；改文件不恢复额度，无缺口的 Stop 才重置。
# The bridge hardcodes stop_hook_active:false, so the guard counts its own blocks.
STOP_BLOCK_LIMIT = 1


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


# 命令词不是路径；候选按空白切分前先剥离它们 / command words are not paths.
# 引号内的空格属于路径本身，所以先按引号感知的方式切成词 / quotes protect spaces.
SHELL_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _is_separator(token: str) -> bool:
    return bool(token) and all(character in "();|&" for character in token)


def _logical_lines(text: str) -> list[str]:
    """按换行切成逻辑行，但引号内的换行不算 / newlines outside quotes only.

    `python3 -c "` 后面跨好几行的内联脚本是常见写法；按物理行切会把引号切开，
    分词失败后退化成「保守认定有写入」，于是只读命令又开始误报。
    """
    lines: list[str] = []
    current: list[str] = []
    quote = ""
    for character in text:
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == "\n":
            lines.append("".join(current))
            current = []
            continue
        current.append(character)
    lines.append("".join(current))
    return lines


def shell_tokens(text: str) -> list[list[str]] | None:
    """把命令行切成若干条简单命令的词表 / one token list per simple command.

    shlex 的标点模式会把引号里的内容保持成**一个词**，于是 `grep -n "cp " f.py`
    里的 `cp ` 再也不会被当成命令。返回 None 表示引号不配对等无法分词的情况，
    调用方按「保守认定有写入」处理，绝不因为解析失败就放行。
    """
    segments: list[list[str]] = []
    current: list[str] = []
    for line in _logical_lines(strip_heredocs(text)):
        lexer = shlex.shlex(line, posix=True, punctuation_chars="();<>|&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""  # 反斜杠是 Windows 路径的一部分，不是转义符
        try:
            tokens = list(lexer)
        except ValueError:
            return None
        for token in tokens:
            if _is_separator(token):
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append(token)
        if current:  # 换行也是命令分隔符
            segments.append(current)
            current = []
    return segments


def _path_args(tokens: list[str]) -> list[str]:
    """选项与命令词不是路径；只留看起来像路径的词 / only path-looking words."""
    return [token for token in tokens
            if not token.startswith("-") and token.lower() not in SHELL_WRITE_VERBS
            and ("/" in token or "\\" in token)]


# 这些命令只写最后那个参数（源文件不动）/ only the last argument is written.
SHELL_LAST_TARGET = frozenset({"cp"})
# 这些命令的每个参数都算改动（`mv` 会把源文件删掉）/ every argument changes.
SHELL_ALL_TARGETS = frozenset({"mv", "ln", "tee", "touch", "rm", "rmdir", "mkdir", "truncate"})


def _write_targets(command: str, rest: list[str]) -> list[str]:
    """按命令语义找出写入目标，包括不含 `/` 的裸文件名 / bare names count too.

    交接文件常常就在项目根（`handoff-current.md`、`README.md`），名字里没有斜杠；
    只收「像路径」的词会把它们全漏掉，于是更新了 dashboard 也不算数。
    """
    args = [token for token in rest
            if not token.startswith("-") and not REDIRECT_RE.match(token)
            and token.lower() not in SHELL_SINKS]
    if not args:
        return []
    if command in SHELL_LAST_TARGET:
        return args[-1:]
    if command in SHELL_ALL_TARGETS:
        return args
    if command == "sed":
        # 第一个非选项参数是 sed 脚本（如 `s/a/b/`），后面的才是目标文件。
        return args[1:]
    return []


def _command_head(tokens: list[str]) -> int:
    """跳过变量赋值、sudo/env/xargs 一类前缀，返回真正命令的下标。"""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if SHELL_ASSIGN_RE.match(token) or token.lower() in SHELL_WRAPPER:
            index += 1
            while index < len(tokens) and (tokens[index].startswith("-") or re.fullmatch(r"\d+[smhd]?", tokens[index])):
                index += 1
            continue
        return index
    return len(tokens)


def _segment_writes(segment: list[str]) -> tuple[bool, list[str]]:
    """一条简单命令是否写入、写到哪 / does this simple command write, and where."""
    writes = False
    paths: list[str] = []
    for index, token in enumerate(segment):
        if REDIRECT_RE.match(token):
            if index + 1 < len(segment):
                target = segment[index + 1]
                # 丢弃到 /dev/null、NUL 之类的黑洞不是改了项目文件。
                if target.lower() not in SHELL_SINKS:
                    writes = True
                    paths.append(target)
    head = _command_head(segment)
    if head >= len(segment):
        return writes, paths
    command = segment[head].lower()
    rest = segment[head + 1:]
    if command == "dd":
        # dd 的目标写在 of= 里 / dd names its target with of=.
        targets = [token[3:] for token in rest if token.startswith("of=") and len(token) > 3]
        targets = [item for item in targets if item.lower() not in SHELL_SINKS]
        writes = writes or bool(targets)
        paths.extend(targets)
    elif command in SHELL_WRITE_COMMANDS:
        if command in SHELL_LAST_TARGET or command in SHELL_ALL_TARGETS:
            # 认得语义就只信语义，不再退回「像路径的词」，否则 `tee /dev/null` 又会被收进来。
            targets = _write_targets(command, rest)
        else:
            # cat/echo/printf 之类只有配重定向才写，目标已由重定向分支收走。
            targets = _path_args(rest)
        # 全部目标都是黑洞时，这条命令没有改任何项目文件。
        if targets or command not in SHELL_LAST_TARGET | SHELL_ALL_TARGETS:
            writes = True
        paths.extend(targets)
    elif command == "sed" and any(token.startswith(("-i", "--in-place")) for token in rest):
        writes = True
        paths.extend(_write_targets("sed", rest))
    elif command == "git" and "apply" in [token.lower() for token in rest]:
        writes = True
        paths.extend(_path_args(rest))
    elif command in SHELL_NESTED and "-c" in rest:
        inner = rest[rest.index("-c") + 1:]
        nested = _shell_writes(inner[0]) if inner else None
        if nested is not None:
            writes = writes or nested[0]
            paths.extend(nested[1])
    elif command == "find":
        # find 的 -exec/-ok 后面又是一条命令 / find re-enters command position.
        for index, token in enumerate(rest):
            if token.lower() in SHELL_REEXEC and index + 1 < len(rest):
                nested = _shell_writes(" ".join(rest[index + 1:]))
                if nested is not None:
                    writes = writes or nested[0]
                    paths.extend(nested[1])
                break
    return writes, paths


def _shell_writes(text: str) -> tuple[bool, list[str]] | None:
    segments = shell_tokens(text)
    if segments is None:
        return None
    writes = False
    paths: list[str] = []
    for segment in segments:
        found, targets = _segment_writes(segment)
        writes = writes or found
        paths.extend(targets)
    return writes, paths


def shell_is_write(text: str) -> bool:
    """命令位置上的写入动词或重定向才算写入；分词失败时保守认定是写入。"""
    result = _shell_writes(text)
    return True if result is None else result[0]


def shell_paths(text: str) -> list[str]:
    """收集保守的路径线索 / conservative path hints.

    `cp a.md papers/x.md` 曾被整条命中成一条「路径」、目标反而漏判；按空白硬拆
    又会把 `"papers/Chapter 2 draft.md"` 拆碎。现在按引号感知分词后再筛。
    """
    segments = shell_tokens(text)
    if segments is None:  # 分词失败时退回旧的正则切词
        return [word.strip("\"'") for word in SHELL_TOKEN_RE.findall(text)
                if not word.strip("\"'").startswith("-")
                and word.strip("\"'").lower() not in SHELL_WRITE_VERBS
                and ("/" in word or "\\" in word)]
    return _path_args([token for segment in segments for token in segment])


HEREDOC_OPEN_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def strip_heredocs(text: str) -> str:
    """删掉 heredoc 正文，只留下真正会被 shell 执行的命令行。

    heredoc 正文里写着的 `cp a.md papers/b.md` 只是文字，不是命令；整段搜索会把
    它当成真的写入，于是「用 heredoc 写一段说明」就足以让守卫认定成果文件被改动。
    A heredoc body is data, not commands: only the opening line may write.
    """
    lines = text.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC_OPEN_RE.finditer(line):
            terminator = match.group(2)
            while index < len(lines) and lines[index].strip() != terminator:
                index += 1
            index += 1  # 连同结束行一起跳过
    return "\n".join(kept)


def shell_write_paths(text: str) -> list[str]:
    """只在实际执行写入的那条简单命令里找目标路径。

    整段命令行搜索会把 heredoc 正文里**只是被提到**的路径当成被写入的文件：
    一条 `cp a.md /tmp/` 加一段提到 `literature/...` 的 python heredoc，就足以让
    守卫认定成果文件被改动，之后无论怎么更新交接都清不掉。
    Only the simple command that actually writes may contribute target paths;
    a path merely mentioned inside a heredoc body is not a write.
    """
    result = _shell_writes(text)
    return shell_paths(text) if result is None else result[1]


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
        # 内联 Python 先按语法读目标；读不出来再退回命令位置判定。
        # `python3 - <<'PY' … p.write_text(…)` 是最常见的改文件写法，只按 shell
        # 命令看会漏掉整个写入，于是改完文件却不算数，回合永远收不了尾。
        python_paths = python_write_paths(text)
        if python_paths:
            paths = python_paths
        else:
            # 只看真正会被执行的部分：heredoc 正文是数据，不是命令。
            executed = strip_heredocs(text)
            if not shell_is_write(executed):
                return result
            paths = shell_write_paths(executed)
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
            # `Path(...)` 与 `pathlib.Path(...)` 是同一个东西；漏了后者会让
            # 「先 pathlib.Path(...) 再 write_text」这种最平常的写法查不到目标。
            if isinstance(node.func, ast.Name) and node.func.id in ("Path", "open") and node.args:
                return value(node.args[0])
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in ("resolve", "absolute", "expanduser"):
                    return value(node.func.value)
                if node.func.attr in ("Path", "path") and node.args:
                    return value(node.args[0])
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
            # def/class 只绑定自己的名字，assert/pass 既不写文件也不重新绑定任何名字；
            # 把它们当成未解释控制流清空变量，会让「先定路径、再定义 helper 或断言、
            # 最后写文件」这种常见写法漏判——真实触发是用 heredoc 改 handoff-current.md。
            if not isinstance(statement, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                          ast.AsyncFunctionDef, ast.ClassDef,
                                          ast.Assert, ast.Pass)):
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
        # 同样的命令位置判定：只在真正执行的简单命令里认路径。
        executed = strip_heredocs(text)
        if not shell_is_write(executed):
            return []
        try:
            # 明显 shell 写入仍受实际内容差异约束；兼容裸 dashboard 文件名。
            targets = {proto.normalise(item) for item in [*shell_paths(executed), *shlex.split(executed)]}
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
        blocks = int(state.get("stop_blocks", 0))
        retry = payload.get("stop_hook_active") is not True and blocks < STOP_BLOCK_LIMIT
        message = "Handoff guard: " + "; ".join(issues)
        if retry:
            state["stop_blocks"] = blocks + 1
            message += (" Make at most one bounded handoff correction, then finish this turn. "
                        "If still unresolved, report the gap in your final response. "
                        "Do not keep rewriting handoffs or debug/modify the guard just to pass this check.")
            print(json.dumps({"decision": "block", "reason": message}, ensure_ascii=False))
        elif not state.get("stop_warned"):
            state["stop_warned"] = True
            message += "; automatic retries stopped; handoff is still unresolved; report the gap and finish this turn"
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
