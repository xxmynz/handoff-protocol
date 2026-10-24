# -*- coding: utf-8 -*-
"""DSH 全局 hook 入口 / one global hook entry for every DSH project.

宿主的 hook 桥只能配一个 configPath（profile 级），而协议脚本必须由**目标项目
自己**的副本运行——`protocol.load()` 默认把 `__file__` 的上一级当项目根，跑模板副本
会把别的项目按模板的 writePolicy 分类。这个入口按会话工作区找到项目根，只在该根存在
`.agent-workspace.json` 时把 stdin 原样转交给该项目的 `scripts/protocol_*.py`。
没有该配置的项目（例如 D:\\投资工作站）永远不被触碰。

A host hook bridge takes ONE configPath for the whole profile, but the protocol
scripts must run from the target project's own copy (`protocol.load()` defaults
to `__file__`'s grandparent). This entry resolves the project root from the
session workdir and delegates over stdin only when that root carries
`.agent-workspace.json`; every other project is left alone.

Always exits 0: a hook must never block the host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CONFIG_NAME = ".agent-workspace.json"
SCRIPTS = {
    "SessionStart": "protocol_context.py",
    "PostToolUse": "protocol_guard.py",
    "Stop": "protocol_guard.py",
}
MAX_PARENT_HOPS = 8  # /mnt/d/<project> needs 3 upward hops; a deep subdir still lands.
CHILD_TIMEOUT_SECONDS = 8
LOG_NAME = "protocol-dispatch.log"
LOG_LIMIT_BYTES = 512 * 1024
WINDOWS_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def log(message: str) -> None:
    """追加一行诊断；日志是宿主机 hook 唯一可见的证据通道。"""
    try:
        path = Path(tempfile.gettempdir()) / LOG_NAME
        if path.exists() and path.stat().st_size > LOG_LIMIT_BYTES:
            path.unlink()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def mnt_form(value: str) -> str | None:
    """把 Windows 盘符或当前 WSL 发行版的 UNC 工作区映射为 Linux 路径。"""
    unc = re.match(r"^//(?:wsl\.localhost|wsl\$)/([^/]+)(/.*)?$", value.replace("\\", "/"), re.I)
    if unc:
        distro = os.environ.get("WSL_DISTRO_NAME")
        if not distro or unc.group(1).casefold() != distro.casefold():
            return None  # 别的发行版不是本解释器的文件系统，不能误接入同名目录。
        return unc.group(2) or "/"
    match = WINDOWS_PATH_RE.match(value)
    if match is None:
        return None
    drive, rest = match.groups()
    rest = rest.replace("\\", "/")
    return f"/mnt/{drive.lower()}/{rest}"


def candidate_roots(payload: dict[str, Any]) -> list[str]:
    """会话工作区的几个等价来源 / every equivalent source of the session workdir."""
    raw: list[str] = []
    try:
        raw.append(os.getcwd())
    except OSError:
        pass
    for key in ("CLAUDE_PROJECT_DIR", "PROTOCOL_PROJECT_DIR"):
        value = os.environ.get(key)
        if isinstance(value, str) and value.strip():
            raw.append(value)
    value = payload.get("cwd")
    if isinstance(value, str) and value.strip():
        raw.append(value)

    roots: list[str] = []
    for item in raw:
        for form in (item, mnt_form(item)):
            if form and form not in roots:
                roots.append(form)
    return roots


def find_project(roots: list[str]) -> Path | None:
    """向上寻找带 `.agent-workspace.json` 的项目根 / nearest adopted project root."""
    for root in roots:
        try:
            start = Path(root).expanduser()
            if not start.is_absolute():
                start = Path.cwd() / start
            start = start.resolve()
        except OSError:
            continue
        for depth, candidate in enumerate([start, *start.parents]):
            if depth > MAX_PARENT_HOPS:
                break
            if (candidate / CONFIG_NAME).is_file():
                return candidate
    return None


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    event = payload.get("hook_event_name")
    script_name = SCRIPTS.get(str(event))
    if script_name is None:
        return 0

    root = find_project(candidate_roots(payload))
    if root is None:
        log(f"{event} skip: no {CONFIG_NAME} above the session workdir")
        return 0

    script = root / "scripts" / script_name
    if not script.is_file():
        log(f"{event} skip: {root} has no scripts/{script_name}")
        return 0

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    env["PROTOCOL_PROJECT_DIR"] = str(root)
    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            input=raw,
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log(f"{event} failed: {script_name} in {root}: {error}")
        return 0

    sys.stdout.buffer.write(completed.stdout)
    sys.stdout.buffer.flush()
    if completed.stderr:
        sys.stderr.buffer.write(completed.stderr)
        sys.stderr.buffer.flush()
    log(f"{event} ran {script_name} in {root} (exit {completed.returncode}, {len(completed.stdout)}B stdout)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
