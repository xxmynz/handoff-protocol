# -*- coding: utf-8 -*-
"""自检 / self-check for protocol_guard: run `python3 scripts/protocol_guard_selftest.py`.

不需要测试框架；退出码 0 表示全部通过。用真实 DSH 桥 payload 形状（tool_response 是压平后
的纯文本）驱动 classify_write / is_successful_tool / handle_stop，全部在临时项目里跑，
不碰任何真实项目。覆盖曾经会漏掉的场景：正文本身带 "Traceback"、`>` / `>>` / `tee` 写入、
`2>&1` 不算写入、Stop 连续拦截有上限。

Standard library only; exit code 0 means every check passed. Payloads mirror the DSH
bridge (tool_response is flattened text) and everything runs inside a throwaway
project, so no real workspace is touched.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol  # noqa: E402
import protocol_guard as guard  # noqa: E402

SESSION = "selftest-session"
CONFIG = {
    "version": 1,
    "name": "guard-selftest",
    "marker": ".agent-workspace-managed",
    "dashboard": {"file": "handoff-current.md", "limitBytes": 5120},
    "handoffs": {"active": "handoffs/active/", "archive": "handoffs/archive/"},
    "memory": {"index": "memory/INDEX.md"},
    "writePolicy": {
        "material": {"rootFiles": ["agents.md"], "prefixes": ["memory/", "problems/", "scripts/"]},
        "ignored": {"prefixes": ["tmp/"], "contains": ["/raw/"]},
        "advisory": {"prefixes": ["scratch/"]},
        "ordinary": {"rootFiles": [".gitignore"]},
    },
}
DANGLING_DASHBOARD = "## Active/waiting workstreams\n\n- `handoffs/active/does-not-exist.md`\n"
FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   " + label)
        return
    FAILURES.append(label)
    print("  FAIL " + label + " " + detail)


def payload(tool_name: str, tool_input: dict, response: str = "ok", cwd: str = "") -> dict:
    return {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "cwd": cwd,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": response,
    }


def run() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="protocol-guard-selftest-")).resolve()
    (workspace / ".agent-workspace.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (workspace / "handoffs" / "active").mkdir(parents=True)
    (workspace / "memory" / "stable").mkdir(parents=True)
    (workspace / "handoff-current.md").write_text(DANGLING_DASHBOARD, encoding="utf-8")
    proto = protocol.load(workspace)
    root = guard.temp_root(proto.name, proto.root)
    state = guard.state_path(root, SESSION)

    try:
        print("classify_write: 写入必须被看见 / writes must be seen")
        item = payload("write", {"file_path": "problems/0001-x.md"}, "Traceback (most recent call last): ... exit code: 1")
        check("native write, content mentions Traceback",
              guard.classify_write(proto, item)["material"] == ["problems/0001-x.md"], str(guard.classify_write(proto, item)))
        item = payload("edit", {"file_path": "memory/stable/fact.md"}, "-old\n+new")
        check("native edit", guard.classify_write(proto, item)["material"] == ["memory/stable/fact.md"])
        for command in ("printf hi > problems/0002-y.md", "echo hi >> problems/0002-y.md", "echo hi | tee problems/0002-y.md"):
            item = payload("bash", {"command": command}, "")
            check("shell write: " + command, guard.classify_write(proto, item)["material"] == ["problems/0002-y.md"], str(guard.classify_write(proto, item)))

        print("classify_write: 只读与失败必须不被看见 / reads and failures stay invisible")
        cases = (
            ("read via tail", payload("bash", {"command": "tail -n 5 problems/0001-x.md"}, "Traceback")),
            ("2>&1 is not a write", payload("bash", {"command": "ls -la 2>&1 | head -3"}, "")),
            ("failed native write", payload("write", {"file_path": "problems/0003-z.md"}, "Error: ENOENT: no such file or directory")),
            ("failed shell", payload("bash", {"command": "printf hi > problems/0004.md"}, "Traceback (most recent call last): boom")),
            ("shell exit code 1", payload("bash", {"command": "printf hi > problems/0005.md"}, "x\nexit code: 1")),
            ("ignored path", payload("write", {"file_path": "tmp/scratch.md"}, "diff")),
            ("unrelated tool", payload("read", {"file_path": "problems/0001-x.md"}, "diff")),
        )
        for label, item in cases:
            check(label, not any(guard.classify_write(proto, item).values()), str(guard.classify_write(proto, item)))
        # 路径缺失时不得静默放行：必须留下 unresolved 标记 / a missing path must not pass silently.
        marked = guard.classify_write(proto, payload("write", {}, "diff"))
        check("file tool without a path is flagged, not ignored",
              marked["ordinary"] == ["unresolved file-tool write"] and not marked["material"], str(marked))

        print("temp_root: 同名克隆不得共享状态 / same-named clones stay isolated")
        check("the project root hashes into the state directory",
              guard.temp_root(proto.name, Path("/a/x")) != guard.temp_root(proto.name, Path("/b/x")))

        print("Stop: 连续拦截有上限 / the block loop is bounded")
        state.unlink(missing_ok=True)
        decisions = []
        # 去重要求取代旧同状态反复 block 预期；真实变化才允许再次重试。
        for index in range(guard.STOP_BLOCK_LIMIT + 1):
            (workspace / "handoff-current.md").write_text(DANGLING_DASHBOARD + "x" * index, encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                guard.handle_stop(proto, state, {"hook_event_name": "Stop", "stop_hook_active": False})
            decisions.append(json.loads(buffer.getvalue() or "{}"))
            if index == 0:
                repeat, diagnostic = io.StringIO(), io.StringIO()
                with redirect_stdout(repeat), redirect_stderr(diagnostic):
                    guard.handle_stop(proto, state, {"stop_hook_active": False})
                notice = json.loads(repeat.getvalue() or "{}")
                check("unchanged state warns instead of blocking again",
                      "systemMessage" in notice and "decision" not in notice and "handoff is still unresolved" in diagnostic.getvalue())
                repeat, diagnostic = io.StringIO(), io.StringIO()
                with redirect_stdout(repeat), redirect_stderr(diagnostic):
                    guard.handle_stop(proto, state, {"stop_hook_active": False})
                check("unchanged state does not repeat the warning", repeat.getvalue() == diagnostic.getvalue() == "")
                check("unchanged state spends only one block", guard.load_state(state).get("stop_blocks") == 1)
        check("the first %d stops block" % guard.STOP_BLOCK_LIMIT,
              all(item.get("decision") == "block" for item in decisions[: guard.STOP_BLOCK_LIMIT]), str(decisions))
        check("the next stop only warns", "systemMessage" in decisions[-1] and "decision" not in decisions[-1], str(decisions))

        print("main(): stdin -> stdout 端到端 / end to end through main()")
        state.unlink(missing_ok=True)
        stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(payload("write", {"file_path": "problems/0006-e2e.md"}, "diff", str(workspace))) + "\n")
            buffer = io.StringIO()
            # main 默认按脚本落点加载；单元测试必须显式绑定临时项目，不能读真实仓库。
            with redirect_stdout(buffer), patch.object(protocol, "load", return_value=proto):
                code = guard.main()
            check("PostToolUse exits 0 and stays quiet", code == 0 and buffer.getvalue() == "", buffer.getvalue())

            sys.stdin = io.StringIO(json.dumps({"session_id": SESSION, "hook_event_name": "Stop", "stop_hook_active": False}) + "\n")
            buffer = io.StringIO()
            # main 默认按脚本落点加载；单元测试必须显式绑定临时项目，不能读真实仓库。
            with redirect_stdout(buffer), patch.object(protocol, "load", return_value=proto):
                code = guard.main()
            output = json.loads(buffer.getvalue() or "{}")
            check("a material write without a handoff update blocks the stop", output.get("decision") == "block", buffer.getvalue())
        finally:
            sys.stdin = stdin

        print("")
        if FAILURES:
            print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
            return 1
        print("all checks passed")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
