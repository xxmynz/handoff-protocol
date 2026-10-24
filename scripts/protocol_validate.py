# -*- coding: utf-8 -*-
"""通用协议校验器 / generic protocol validator.

校验所有配置驱动的、与领域无关的约束：文件预算、dashboard 完整性、adoption marker、
禁用硬编码本机根目录，以及项目在 `policy` 中声明的 skill 策略短语。
可选的项目自有规则放在 `scripts/project_policy.py`（模板给空壳）。

Validates every config-driven, domain-neutral constraint. Repository-specific rules belong
in an optional `scripts/project_policy.py`, which this validator calls when present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol  # noqa: E402  (local sibling module)
import protocol_guard  # noqa: E402  (local sibling module)

CONTROL_FILES = (
    "AGENTS.md",
    "AGENT-BOOTSTRAP.md",
    "README.md",
)
SCRIPTS_DIR = "scripts"

# 配置里允许出现的键。未知键必须报错：写错一个字母的 writePolicy 键会被静默忽略，
# 于是"改了重要文件却不要求交接"，而且没人发现——这正是校验器要拦住的那类错误。
KNOWN_CONFIG_KEYS: dict[str, set[str]] = {
    "": {
        "version",
        "name",
        "marker",
        "dashboard",
        "handoffs",
        "memory",
        "writePolicy",
        "policy",
        "templates",
    },
    "dashboard": {"file", "limitBytes", "budgets"},
    "handoffs": {"active", "archive"},
    "memory": {"index"},
    "writePolicy": {"material", "ignored", "advisory", "ordinary"},
    "writePolicy.material": {"rootFiles", "prefixes"},
    "writePolicy.ignored": {"prefixes", "contains"},
    "writePolicy.advisory": {"prefixes"},
    "writePolicy.ordinary": {"rootFiles", "prefixes"},
}


def config_key_problems(config_path: Path) -> list[str]:
    """报告配置里的未知键 / report unknown keys in the protocol config."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    problems: list[str] = []

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        known = KNOWN_CONFIG_KEYS.get(prefix)
        if known is None:
            return
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in known:
                problems.append(f"unknown key in {protocol.CONFIG_NAME}: {path}")
                continue
            walk(value, path)

    walk(raw, "")
    return problems


def collect_problems(proto: protocol.Protocol) -> list[str]:
    """本协议的全部校验项 / every protocol validation check.

    唯一实现：CLI（`main`）与 MCP `validate_handoff` 都调这里。以前两边各抄一份，
    结果 MCP 侧漏掉了 `config_key_problems`——agent 只走 MCP 时，写错一个键名会被报
    "通过"。任何新校验项都只加在这个函数里。
    The single implementation behind both the CLI and the MCP tool.
    """
    problems: list[str] = []
    problems.extend(config_key_problems(proto.root / protocol.CONFIG_NAME))
    problems.extend(protocol_guard.dashboard_issues(proto))

    # 文件大小门槛 / size budgets.
    for relative, limit in (
        {proto.dashboard: proto.dashboard_limit_bytes} | dict(proto.budgets)
    ).items():
        path = proto.path(relative)
        if not path.exists():
            problems.append(f"missing {relative}")
        elif path.stat().st_size > limit:
            problems.append(f"{relative}: {path.stat().st_size} bytes > {limit}")

    # adoption marker：声明该仓库采用本协议 / proves this repo opted into the protocol.
    if not proto.path(proto.marker).exists():
        problems.append(f"missing adoption marker {proto.marker}")

    # 启动控制文件不得写死本机项目根目录 / no host-specific project root.
    controls = [proto.path(name) for name in CONTROL_FILES]
    controls.extend(sorted((proto.root / SCRIPTS_DIR).glob("*.py")))
    root_text = proto.root.as_posix()
    for path in controls:
        if path.exists() and root_text.casefold() in path.read_text(encoding="utf-8").casefold():
            problems.append(f"host-specific project root in {_relative(proto, path)}")

    # 项目声明的策略短语 / repository-declared policy phrases.
    for entry in proto.policy:
        skill = str(entry.get("skill"))
        skill_file = proto.root / "skills" / skill / "SKILL.md"
        if not skill_file.exists():
            problems.append(f"policy references missing skill: {skill}")
            continue
        text = skill_file.read_text(encoding="utf-8")
        for phrase in entry.get("phrases") or []:
            if phrase not in text:
                problems.append(f"{skill}: missing policy phrase {phrase!r}")

    # 可选的项目自有规则 / optional repository-owned rules.
    try:
        import project_policy  # type: ignore[import-not-found]
    except ImportError:
        project_policy = None
    if project_policy is not None and hasattr(project_policy, "check"):
        problems.extend(project_policy.check(proto))

    return problems


def _relative(proto: protocol.Protocol, path: Path) -> str:
    try:
        return path.relative_to(proto.root).as_posix()
    except ValueError:
        return path.as_posix()


def skeleton_target(proto: protocol.Protocol) -> Path | None:
    """模板仓库：配置里声明 templates 目录时，校验骨架而非仓库根。

    Template repository: when the config declares a templates directory, validate that
    skeleton instead of the repository root. CLI 与 MCP 共用，两边结论一致。
    """
    config = proto.root / protocol.CONFIG_NAME
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    block = raw.get("templates") if isinstance(raw, dict) else None
    if isinstance(block, str) and block.strip():
        target = proto.root / block.strip()
        return target if target.is_dir() else None
    if isinstance(block, dict):
        target = proto.root / str(block.get("dir") or "templates")
        return target if target.is_dir() else None
    return None


def _template_dir(proto: protocol.Protocol) -> Path | None:
    """向后兼容的旧名字 / kept for callers that used the old name."""
    return skeleton_target(proto)




def main() -> int:
    try:
        root_proto = protocol.load()
    except protocol.ConfigError as error:
        print("VALIDATION FAILED")
        print(f"- {error}")
        return 1

    target = skeleton_target(root_proto)
    if target is None:
        proto = root_proto
        subject = ""
    else:
        try:
            proto = protocol.load(target)
        except protocol.ConfigError as error:
            print("VALIDATION FAILED")
            print(f"- template skeleton: {error}")
            return 1
        subject = f" (template skeleton: {target.relative_to(root_proto.root).as_posix()})"

    problems = collect_problems(proto)

    if problems:
        print("VALIDATION FAILED")
        for problem in problems:
            print(f"- {problem}")
        return 1

    print(f"VALIDATION PASSED{subject}")
    for relative in dict(proto.budgets):
        path = proto.path(relative)
        if path.exists():
            print(f"- {relative}: {path.stat().st_size} bytes")
    dashboard = proto.path(proto.dashboard)
    print(f"- {proto.dashboard}: {dashboard.stat().st_size} bytes")
    print(f"- policy entries: {len(proto.policy)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
