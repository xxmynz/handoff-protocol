#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协议 MCP 服务 / protocol MCP server.

把协议暴露为 agent 可调用的工具，与 hook **无关**：任何支持 MCP 的宿主都能调用，
因此不依赖 DSH 的 hook 桥接。

Exposes the protocol as agent-callable tools, independent of hooks, so any MCP-capable
host can use it. Zero third-party dependencies; JSON-RPC 2.0 over stdio, one JSON object
per line.

工具 / Tools:
  adopt_workspace    — 补项目协议文件；保留交接正文，但会同步覆盖同名协议脚本
  handoff_status     — 当前 dashboard、active workstream 与校验问题
  validate_handoff   — 跑完整校验（预算 / 链接 / marker / policy）
  archive_workstream — 把完成的 workstream 归档并从 dashboard 移除（默认 dry-run）
  describe_policy    — 回报写分级规则，让 agent 知道什么会触发交接要求

`adopt_workspace` 让"接入协议"从一条终端命令变成一次工具调用：用户只要在会话里说
"把这个项目接入协议"，agent 就能带着当前工作目录直接调用它。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import protocol  # noqa: E402
import protocol_adopt  # noqa: E402
import protocol_guard  # noqa: E402
import protocol_validate  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agent-protocol"
SERVER_VERSION = "1.0.0"

ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


# --------------------------------------------------------------------------- 核心逻辑


def translate_workspace(value: str) -> Path | None:
    """把 Windows 风格路径转成 WSL 可见路径 / map a Windows path into WSL.

    宿主可能传 `C:\\repo`、`D:\\投资工作站` 或 `\\\\wsl.localhost\\Ubuntu\\home\\...`；
    这些在 WSL 内都不存在，必须先翻译，否则工具永远找不到配置。
    """
    text = value.strip().strip("\"'")
    if not text:
        return None
    unc = re.match(r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\(.*)$", text, re.IGNORECASE)
    if unc:
        rest = unc.group(2).replace("\\", "/")
        if rest.lower().startswith("home/"):
            return Path("/" + rest)
        return Path("/" + rest)
    drive = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if drive:
        return Path("/mnt/{}/{}".format(drive.group(1).lower(), drive.group(2).replace("\\", "/")))
    return Path(text).expanduser()


def _has_config(path: Path) -> bool:
    try:
        return (path / protocol.CONFIG_NAME).is_file()
    except OSError:
        return False


def discover_root() -> Path:
    """寻找协议根：先从 cwd 向上，再退回脚本自身位置。

    不假设进程工作目录正确，也不假设宿主传对了路径——任一环节失效都还能找到。
    """
    for base in (Path.cwd(), HERE):
        for candidate in (base, *base.parents):
            if _has_config(candidate):
                return candidate
    return HERE.parent


def resolve_root(workspace: str | None) -> Path:
    """确定工作区根：显式参数（含 Windows 路径）优先，否则自动发现。"""
    import os

    explicit = (workspace or os.environ.get("AGENT_PROTOCOL_WORKSPACE") or "").strip()
    if explicit:
        translated = translate_workspace(explicit)
        if translated is not None:
            return translated
    return discover_root()


def load_protocol(workspace: str | None) -> protocol.Protocol:
    """加载协议配置 / load the protocol config for the resolved workspace."""
    return protocol.load(resolve_root(workspace))


def adopt_workspace(
    workspace: str | None, dry_run: bool = True, name: str | None = None
) -> dict[str, Any]:
    """把协议装进一个项目 / adopt the protocol into a project.

    与 `protocol_adopt.py` 共用同一份计划与写入逻辑（同一个 scripts 目录里 import 它），
    所以命令行与会话内两条路不会各说各话。已存在的 AGENTS.md / handoff / memory
    一律不动；同名协议脚本会同步覆盖，配置中明确废弃的键也可能被清理。
    """
    explicit = (workspace or "").strip()
    if not explicit:
        return {
            "ok": False,
            "error": "workspace is required",
            "hint": "传目标项目目录的绝对路径，例如 /home/user/projects/my-app 或 D:\\my-app",
        }
    templates = protocol_adopt.find_templates()
    if templates is None or not (templates / protocol.CONFIG_NAME).exists():
        return {
            "ok": False,
            "error": "template skeleton not found",
            "hint": (
                "把模板仓库放到 ~/projects/agent-protocol-template，或用环境变量 "
                "AGENT_PROTOCOL_TEMPLATE 指向它"
            ),
        }

    target = translate_workspace(explicit)
    if target is None:
        return {"ok": False, "error": f"cannot resolve workspace: {explicit}"}
    target = Path(target).expanduser()
    try:
        target = target.resolve()
    except OSError as error:
        return {"ok": False, "error": f"cannot resolve workspace: {error}"}
    if target == templates.parent.resolve():
        return {
            "ok": False,
            "error": "target is the template repository itself",
            "hint": "指向真正要接入的项目目录",
        }
    if target.exists() and not target.is_dir():
        return {"ok": False, "error": f"not a directory: {target}"}

    project_name = (name or "").strip() or target.name
    adopted = (target / protocol.CONFIG_NAME).exists()
    plan = protocol_adopt.build_plan(target, templates, project_name, adopted)
    report: dict[str, Any] = {
        "ok": True,
        "workspace": str(target),
        "project": project_name,
        "already_adopted": adopted,
        "would_create": sorted(plan[protocol_adopt.CREATE_FILES]),
        "would_update": sorted(plan[protocol_adopt.UPDATE_FILES]),
        "kept_untouched": sorted(plan[protocol_adopt.KEEP_FILES]),
        "would_prune": sorted(plan[protocol_adopt.PRUNE_KEYS]),
    }

    if dry_run:
        return {**report, "dry_run": True, "hint": "再调一次，dry_run=false 才真正写入"}

    if (
        not plan[protocol_adopt.CREATE_FILES]
        and not plan[protocol_adopt.UPDATE_FILES]
        and not plan[protocol_adopt.PRUNE_KEYS]
    ):
        try:
            validation = validate_handoff(protocol.load(target))
        except protocol.ConfigError as error:
            validation = {"passed": False, "problems": [str(error)]}
        return {
            **report,
            "dry_run": False,
            "changed": False,
            "note": "已经是最新状态，没有写入",
            "validation": {
                "passed": validation.get("passed"),
                "problems": validation.get("problems"),
            },
        }

    try:
        target.mkdir(parents=True, exist_ok=True)
        protocol_adopt.apply_plan(target, templates, project_name, plan, adopted)
    except OSError as error:
        return {"ok": False, "error": f"write failed: {error}", **report}

    report["dry_run"] = False
    report["changed"] = True
    report["created"] = report.pop("would_create")
    report["updated"] = report.pop("would_update")
    report["pruned"] = report.pop("would_prune")
    try:
        validation = validate_handoff(protocol.load(target))
        report["validation"] = {
            "passed": validation.get("passed"),
            "problems": validation.get("problems"),
        }
    except protocol.ConfigError as error:
        report["validation"] = {"passed": False, "problems": [str(error)]}
    report["next_steps"] = [
        "读 AGENTS.md / AGENT-BOOTSTRAP.md，把项目真实规则写进去（若还是模板内容）",
        "按项目真实目录调整 .agent-workspace.json 的 writePolicy.material / ignored",
        "之后用 handoff_status 交接、validate_handoff 校验",
    ]
    return report


COLUMN_KEYS = {
    "workstream": ("workstream", "name", "工作流"),
    "aliases": ("alias", "关键词"),
    "status": ("status", "状态"),
    "next_action": ("next", "下一"),
    "handoff": ("handoff", "detail", "交接"),
}
POSITIONAL = {"workstream": 0, "aliases": 1, "status": 2, "next_action": 3, "handoff": 4}


def _header_index(header: list[str] | None, field: str) -> int | None:
    if not header:
        return None
    for index, name in enumerate(header):
        if any(key in name for key in COLUMN_KEYS[field]):
            return index
    return None


def _cell(cells: list[str], header: list[str] | None, field: str) -> str:
    """有表头就按表头取；没有表头才退回固定列序。"""
    if header:
        index = _header_index(header, field)
        return cells[index] if index is not None and index < len(cells) else ""
    index = POSITIONAL[field]
    return cells[index] if index < len(cells) else ""


def handoff_status(proto: protocol.Protocol, workspace: str | None = None) -> dict[str, Any]:
    """回报当前交接状态 / current handoff state.

    dashboard 表格按**表头名字**取列，不假定列序——项目自己写的 dashboard 只要表头列名
    能被识别（workstream / aliases / status / next action / detailed handoff）就能正确解析。
    """
    dashboard = proto.path(proto.dashboard)
    workstreams: list[dict[str, str]] = []
    header: list[str] | None = None
    fields = ("workstream", "aliases", "status", "next_action", "handoff")
    if dashboard.exists():
        for line in dashboard.read_text(encoding="utf-8").splitlines():
            if not ROW_RE.match(line):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4 or set(cells[0]) <= {"-", " "}:
                continue
            if cells[0].lower() in {"workstream", "name", "工作流"}:
                header = [cell.lower() for cell in cells]
                continue
            workstreams.append({field: _cell(cells, header, field) for field in fields})

    active_dir = proto.path(proto.handoff_active)
    active_files = sorted(p.name for p in active_dir.glob("*.md")) if active_dir.exists() else []
    return {
        "workspace": str(proto.root),
        "project": proto.name,
        "dashboard": proto.dashboard,
        "dashboard_bytes": dashboard.stat().st_size if dashboard.exists() else 0,
        "dashboard_limit_bytes": proto.dashboard_limit_bytes,
        "workstreams": workstreams,
        "active_handoffs": active_files,
        "issues": protocol_guard.dashboard_issues(proto),
    }


def validate_handoff(proto: protocol.Protocol) -> dict[str, Any]:
    """跑完整校验并返回结构化结果 / run full validation, structured result.

    校验项全部来自 `protocol_validate.collect_problems` —— 与 CLI 逐字同源，不再各写一份。
    模板仓库根目录是空骨架，所以配置声明了 `templates` 时改为校验那份骨架（与 CLI 相同）。
    Every check comes from the shared collector, so the MCP tool can never be weaker than the CLI.
    """
    target = protocol_validate.skeleton_target(proto)
    if target is not None:
        proto = protocol.load(target)
    problems = protocol_validate.collect_problems(proto)
    return {"passed": not problems, "problems": problems}


def describe_policy(proto: protocol.Protocol) -> dict[str, Any]:
    """回报写分级规则 / report the write classification policy."""
    policy = proto.write_policy
    return {
        "project": proto.name,
        "classes": {
            "material": {
                "rootFiles": sorted(policy.material_root_files),
                "prefixes": list(policy.material_prefixes),
                "effect": "requires a detailed handoff and a dashboard update this turn",
            },
            "ignored": {
                "prefixes": list(policy.ignored_prefixes),
                "contains": list(policy.ignored_contains),
                "effect": "never triggers the handoff requirement",
            },
            "advisory": {
                "prefixes": list(policy.advisory_prefixes),
                "effect": "reminder only, never blocks",
            },
            "ordinary": {
                "rootFiles": sorted(policy.ordinary_root_files),
                "prefixes": list(policy.ordinary_prefixes),
                "effect": "reminder only, never blocks",
            },
        },
        "dashboard": proto.dashboard,
        "dashboard_limit_bytes": proto.dashboard_limit_bytes,
    }


def archive_workstream(
    proto: protocol.Protocol, handoff: str, apply: bool = False
) -> dict[str, Any]:
    """归档一个 workstream：移动详细交接并从 dashboard 删除，不留占位行。

    Moves the detailed handoff into the archive and removes its row from the dashboard,
    leaving no placeholder. Dry-run unless apply=True.
    """
    relative = proto.normalise(handoff)
    active_prefix = proto.handoff_active.lower()
    if not relative.startswith(active_prefix):
        return {
            "ok": False,
            "error": f"only files under {proto.handoff_active} can be archived (got {relative})",
        }
    if not relative.endswith(".md"):
        return {"ok": False, "error": "handoff must be a .md file"}

    source = proto.path(relative)
    if not source.exists():
        return {"ok": False, "error": f"{relative} does not exist"}

    name = Path(relative).name
    destination_relative = f"{proto.handoff_archive}{name}"
    destination = proto.path(destination_relative)
    if destination.exists():
        return {"ok": False, "error": f"{destination_relative} already exists"}

    dashboard = proto.path(proto.dashboard)
    if not dashboard.exists():
        return {"ok": False, "error": f"{proto.dashboard} is missing"}

    lines = dashboard.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed_rows = 0
    for line in lines:
        if ROW_RE.match(line) and f"`{relative}`" in line:
            removed_rows += 1
            continue
        kept.append(line)
    updated = "\n".join(kept)
    if dashboard.read_text(encoding="utf-8").endswith("\n"):
        updated += "\n"

    result = {
        "ok": True,
        "applied": bool(apply),
        "moved": f"{relative} -> {destination_relative}",
        "dashboard_rows_removed": removed_rows,
        "dashboard": proto.dashboard,
    }
    if not apply:
        result["note"] = "dry-run; pass apply=true to perform the move and dashboard edit"
        return result

    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    dashboard.write_text(updated, encoding="utf-8")
    result["remaining_issues"] = protocol_guard.dashboard_issues(proto)
    return result


# --------------------------------------------------------------------------- MCP 层

TOOLS: list[dict[str, Any]] = [
    {
        "name": "handoff_status",
        "description": (
            "Read the current handoff dashboard: active workstreams, active handoff files, "
            "and any dashboard integrity issues. Call this before acting so you do not "
            "re-derive project state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Absolute path of the protocol workspace (default: the server's project).",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_handoff",
        "description": (
            "Run the full protocol validation for a workspace: size budgets, dashboard links, "
            "archived rows, adoption marker, declared policy phrases and host-specific paths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace path."}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "archive_workstream",
        "description": (
            "Archive one completed workstream: move its detailed handoff into the archive "
            "directory and delete its dashboard row, leaving no placeholder. Dry-run by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handoff": {
                    "type": "string",
                    "description": "Project-relative path under the active handoff directory, e.g. handoffs/active/x.md.",
                },
                "apply": {
                    "type": "boolean",
                    "description": "Set true to perform the move and the dashboard edit. Default false.",
                },
                "workspace": {"type": "string", "description": "Absolute workspace path."},
            },
            "required": ["handoff"],
            "additionalProperties": False,
        },
    },
    {
        "name": "adopt_workspace",
        "description": (
            "Adopt the agent-protocol memory/handoff workspace into a project: creates only the "
            "MISSING config (.agent-workspace.json), marker and protocol scripts; never overwrites "
            "existing AGENTS.md / handoff / memory content. Safe to call when the user asks to "
            "'enable the protocol here' or when the session works in a project that has handoff "
            "docs but no .agent-workspace.json. Dry-run by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Absolute target project directory (Linux, Windows or UNC path).",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Default true: only report the plan. Set false to write.",
                },
                "name": {"type": "string", "description": "Optional project name override."},
            },
            "required": ["workspace"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_policy",
        "description": (
            "Report which paths count as material (require a handoff update), which are ignored, "
            "and which are advisory only, for a given protocol workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Absolute workspace path."}
            },
            "additionalProperties": False,
        },
    },
]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行一个工具并返回结构化结果（不抛异常）。"""
    workspace = arguments.get("workspace")

    # 采用协议是唯一一个"还没有 .agent-workspace.json"也要能跑的工具，先于加载配置派发。
    if name == "adopt_workspace":
        return adopt_workspace(
            workspace,
            dry_run=bool(arguments.get("dry_run", True)),
            name=arguments.get("name"),
        )

    try:
        proto = load_protocol(workspace)
    except protocol.ConfigError as error:
        return {
            "ok": False,
            "not_a_protocol_workspace": True,
            "error": str(error),
            "hint": (
                "This workspace has no .agent-workspace.json, so the protocol does not apply "
                "here. Nothing to do; continue without it."
            ),
        }

    if name == "handoff_status":
        return {"ok": True, **handoff_status(proto)}
    if name == "validate_handoff":
        return validate_handoff(proto)
    if name == "describe_policy":
        return {"ok": True, **describe_policy(proto)}
    if name == "archive_workstream":
        handoff = str(arguments.get("handoff") or "").strip()
        if not handoff:
            return {"ok": False, "error": "handoff is required"}
        return archive_workstream(proto, handoff, bool(arguments.get("apply")))
    return {"ok": False, "error": f"unknown tool: {name}"}


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 请求；通知返回 None。"""
    method = request.get("method")
    request_id = request.get("id")

    if method in {"notifications/initialized", "initialized"}:
        return None
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments must be an object")
        if name not in {tool["name"] for tool in TOOLS}:
            return _error(request_id, -32602, f"unknown tool: {name}")
        payload = call_tool(name, arguments)
        return _result(
            request_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
                ],
                "isError": not payload.get("ok", True),
            },
        )
    if request_id is None:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def serve() -> int:
    """stdio 逐行 JSON-RPC 循环 / newline-delimited JSON-RPC over stdio."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_error(None, -32700, "parse error")), flush=True)
            continue
        if not isinstance(request, dict):
            continue
        response = handle(request)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="默认工作区根目录 / default workspace root")
    parser.add_argument("--selftest", action="store_true", help="跑内置自检后退出")
    args = parser.parse_args()

    if args.selftest:
        return selftest(args.workspace)

    import os

    if args.workspace:
        os.environ["AGENT_PROTOCOL_WORKSPACE"] = str(Path(args.workspace).resolve())
    return serve()


def selftest(workspace: str | None = None) -> int:
    """不依赖宿主的自检 / host-independent self-test."""
    failures: list[str] = []
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    if not init or init["result"]["serverInfo"]["name"] != SERVER_NAME:
        failures.append("initialize failed")
    listing = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in listing["result"]["tools"]} if listing else set()
    expected = {
        "adopt_workspace",
        "handoff_status",
        "validate_handoff",
        "archive_workstream",
        "describe_policy",
    }
    if names != expected:
        failures.append(f"tools/list mismatch: {sorted(names)}")
    for name in sorted(expected - {"adopt_workspace"}):
        response = handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"handoff": "handoffs/active/__none__.md"}},
            }
        )
        if not response or "result" not in response:
            failures.append(f"tools/call {name} failed")
    # adopt_workspace 的守卫只在"脚本住在模板仓库里"时能触发；项目里的脚本副本会正确地
    # 报告 already_adopted。两种部署都必须判为通过，否则自检会在已接入的项目里误报失败。
    # The template-self guard only fires when this script lives in the template repo; a project
    # copy correctly reports already_adopted. Both outcomes are a pass.
    adoption = adopt_workspace(str(HERE.parent), dry_run=True)
    guarded = (
        not adoption.get("ok")
        and "template repository" in str(adoption.get("error") or "")
    )
    if not guarded and not adoption.get("already_adopted"):
        failures.append(f"adopt_workspace guard failed: {adoption}")
    status = call_tool("handoff_status", {"workspace": workspace} if workspace else {})
    if not status.get("ok"):
        failures.append(f"handoff_status not ok: {status.get('error')}")

    if failures:
        print("SELFTEST FAILED")
        for item in failures:
            print(f"- {item}")
        return 1
    print("SELFTEST PASSED")
    print(f"- server: {SERVER_NAME} {SERVER_VERSION}, tools: {sorted(expected)}")
    print(f"- workspace: {status.get('workspace')}")
    print(f"- workstreams: {len(status.get('workstreams') or [])}")
    print(f"- dashboard issues: {len(status.get('issues') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
