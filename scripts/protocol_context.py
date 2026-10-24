# -*- coding: utf-8 -*-
"""会话启动时注入精简项目状态 / concise project context for SessionStart hooks.

注入仅包含 dashboard 的 active 段；守卫另将小型交接文本基线保存在本机临时状态，
不注入模型，不读取原始数据、旧聊天或历史索引。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol  # noqa: E402  (local sibling module)

MAX_CONTEXT_CHARS = 3500
ACTIVE_HEADING_PREFIX = "## active"


def build_context(proto: protocol.Protocol) -> str:
    """提取当前工作主题 / extract only the current workstream dashboard."""
    dashboard = proto.path(proto.dashboard)
    if not dashboard.exists():
        return (
            f"{proto.name} continuity reminder: {proto.dashboard} is missing. "
            "Read the project root and restore the current handoff before editing."
        )

    lines = dashboard.read_text(encoding="utf-8").splitlines()
    selected: list[str] = []
    in_active = False
    for line in lines:
        if line.lower().startswith(ACTIVE_HEADING_PREFIX):
            in_active = True
            selected.append(line)
            continue
        if in_active and line.startswith("## "):
            break
        if in_active:
            selected.append(line)

    active_text = "\n".join(selected).strip()
    return (
        f"{proto.name} continuity reminder:\n"
        f"- Read the compact dashboard before acting: {proto.dashboard}\n"
        "- Route the user's natural-language references to one matching workstream; "
        "the user does not need to provide task IDs.\n"
        "- Open historical indexes only when historical recall is requested.\n\n"
        f"{active_text}"
    )[:MAX_CONTEXT_CHARS]


def main() -> int:
    try:
        proto = protocol.load()
    except protocol.ConfigError as error:
        print(f"Handoff context: {error}", file=sys.stderr)
        return 0
    # hook 才有 stdin payload；手工终端运行不等待输入，也不产生 unknown 会话状态。
    if not sys.stdin.isatty():
        import protocol_guard
        try:
            protocol_guard.start_handoff_observation(proto, protocol_guard.read_payload())
        except OSError as error:
            # 临时状态不可写时仍注入上下文，后续守卫保守处理缺失基线。
            print(f"Handoff observation: {error}", file=sys.stderr)
    # SessionStart hook 输出 JSON / emit the documented SessionStart JSON shape.
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": build_context(proto),
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
