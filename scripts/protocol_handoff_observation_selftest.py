# -*- coding: utf-8 -*-
"""在一次性项目中测试真实脚本进程；参数为待测项目根，不触碰该项目内容。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile


def run(source: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="handoff-observation-") as temporary:
        root = Path(temporary)
        scripts = root / "scripts"
        scripts.mkdir()
        for name in ("protocol.py", "protocol_guard.py", "protocol_context.py", "protocol_validate.py", "protocol_handoff_confirm.py"):
            item = source / "scripts" / name
            if item.exists():
                shutil.copy2(item, scripts / name)
        (root / "handoffs/active").mkdir(parents=True)
        (root / "papers").mkdir()
        (root / ".agent-workspace.json").write_text(json.dumps({
            "version": 1, "name": root.name, "marker": ".managed",
            "dashboard": {"file": "handoff-current.md", "limitBytes": 5120},
            "handoffs": {"active": "handoffs/active/", "archive": "handoffs/archive/"},
            "writePolicy": {"material": {"prefixes": ["papers/"]}},
        }))
        (root / ".managed").touch()
        dashboard = root / "handoff-current.md"
        detail = root / "handoffs/active/task.md"
        dashboard.write_text("## Active/waiting workstreams\n\n- `handoffs/active/task.md`\n")
        detail.write_text("# Task\n\nStatus: In progress\n")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

        def hook(session, event, tool=None, inputs=None, response="ok", stop_active=False):
            payload = {"session_id": session, "hook_event_name": event, "stop_hook_active": stop_active}
            if tool:
                payload.update(tool_name=tool, tool_input=inputs, tool_response=response)
            name = "protocol_context.py" if event == "SessionStart" else "protocol_guard.py"
            result = subprocess.run([sys.executable, str(scripts / name)], input=json.dumps(payload), text=True,
                                    cwd=root, env=env, capture_output=True, check=True)
            return json.loads(result.stdout or "{}")

        def start(session):
            hook(session, "SessionStart")

        def material(session):
            hook(session, "PostToolUse", "write", {"file_path": "papers/result.txt"})

        def stop(session):
            return hook(session, "Stop")

        def python_handoff(session, suffix, execute=True, response="ok", exit_code=0):
            code = "from pathlib import Path\n" + "\n".join(
                f"p=Path({name!r}); p.write_text(p.read_text()+{suffix!r})"
                for name in ("handoff-current.md", "handoffs/active/task.md"))
            if exit_code:
                code += f"\nraise SystemExit({exit_code})"
                if response == "ok":
                    response = f"[Command finished with exit code {exit_code}]"
            if execute:
                result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env)
                assert result.returncode == exit_code
            return hook(session, "PostToolUse", "bash", {"command": "python3 - <<'PY'\n" + code + "\nPY"}, response)

        try:
            probe = "import sys;sys.path.insert(0,'scripts');import protocol_context;from unittest.mock import patch\nwith patch('protocol_guard.save_state', side_effect=PermissionError('probe read-only temp')): protocol_context.main()"
            result = subprocess.run([sys.executable, "-c", probe], input=json.dumps({"session_id": "unwritable"}),
                                    cwd=root, env=env, text=True, capture_output=True, check=True)
            assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            assert "Handoff observation:" in result.stderr
            print("  PASS context still injects when the temporary observation state is unwritable")

            start("missing"); material("missing")
            assert stop("missing").get("decision") == "block"
            assert stop("missing").get("decision") != "block"
            assert stop("missing") == {}
            print("  PASS missing handoff blocks once, then warns once, then stays quiet")

            start("python"); material("python")
            python_handoff("python", "\nPython update\n")
            assert stop("python") == {}
            print("  PASS real inline Python updates both handoffs and clears pending")

            start("native"); material("native")
            for name in ("handoff-current.md", "handoffs/active/task.md"):
                hook("native", "PostToolUse", "edit", {"file_path": name})
            assert stop("native") == {}
            print("  PASS native file tool behavior retained")

            start("old"); python_handoff("old", "\nBefore material\n"); material("old")
            assert stop("old").get("decision") == "block"
            print("  PASS handoffs written before the latest material change do not count")

            start("touch"); material("touch")
            dashboard.touch(); detail.touch()
            hook("touch", "PostToolUse", "bash", {"command": "touch handoff-current.md handoffs/active/task.md"})
            assert stop("touch").get("decision") == "block"
            print("  PASS touch without content change does not count")

            start("absolute-touch"); material("absolute-touch")
            dashboard.touch(); detail.touch()
            hook("absolute-touch", "PostToolUse", "bash", {"command": f"touch {dashboard} {detail}"})
            assert stop("absolute-touch").get("decision") == "block"
            print("  PASS absolute-path touch cannot use legacy path guessing to pass")

            start("noop"); material("noop")
            python_handoff("noop", "")
            assert stop("noop").get("decision") == "block"
            print("  PASS Python rewriting identical content does not count")

            start("other"); material("other")
            start("writer"); python_handoff("writer", "\nAnother session\n")
            assert stop("other").get("decision") == "block"
            hook("other", "PostToolUse", "bash", {"command": "cat handoff-current.md handoffs/active/task.md"})
            assert "systemMessage" in stop("other")
            print("  PASS another session's update or a subsequent read is not credited")

            start("failed"); material("failed")
            python_handoff("failed", "\nPartial failed write\n", response="Error: tool failed")
            assert stop("failed").get("decision") == "block"
            print("  PASS failed tool does not credit partial writes")

            for code in (2, 127):
                session = f"exit-{code}"
                start(session); material(session)
                python_handoff(session, f"\nPartial exit {code}\n", exit_code=code)
                assert stop(session).get("decision") == "block"
            start("structured-exit"); material("structured-exit")
            python_handoff("structured-exit", "\nStructured failure\n", exit_code=2, response={"exitCode": 2})
            assert stop("structured-exit").get("decision") == "block"
            print("  PASS exit 2/127 and structured exitCode reject partial writes")

            start("unrelated-writer"); material("unrelated-writer")
            python_handoff("writer", "\nExternal handoff change\n")
            code = "from pathlib import Path; print('handoff-current.md handoffs/active/task.md'); Path('unrelated.txt').write_text('note')"
            subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, check=True)
            hook("unrelated-writer", "PostToolUse", "bash", {"command": "python3 -c " + shlex.quote(code)})
            assert stop("unrelated-writer").get("decision") == "block"
            print("  PASS printing handoff names while writing elsewhere cannot claim external changes")

            for label, body in (
                ("lambda", "x=lambda: Path('handoff-current.md').write_text('x'); y=lambda: Path('handoffs/active/task.md').write_text('x'); Path('unrelated.txt').write_text('note')"),
                ("branch", "with open('unrelated.txt','w'):\n    if False:\n        Path('handoff-current.md').write_text('x')\n        Path('handoffs/active/task.md').write_text('x')"),
                ("short-circuit", "False and Path('handoff-current.md').write_text('x'); False and Path('handoffs/active/task.md').write_text('x'); Path('unrelated.txt').write_text('note')"),
                ("read-rplus", "with open('handoff-current.md','r+') as f: f.read()\nwith open('handoffs/active/task.md','r+') as f: f.read()\nPath('unrelated.txt').write_text('note')"),
                ("rebound", "p=Path('handoff-current.md'); q=Path('handoffs/active/task.md')\nif True:\n    p=Path('unrelated.txt'); q=Path('another.txt')\np.write_text('note'); q.write_text('note')"),
            ):
                session = "not-executed-" + label
                start(session); material(session)
                python_handoff("writer", f"\nExternal change before {label}\n")
                code = "from pathlib import Path\n" + body
                subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True)
                hook(session, "PostToolUse", "bash", {"command": "python3 - <<'PY'\n" + code + "\nPY"})
                assert stop(session).get("decision") == "block"
            print("  PASS unexecuted lambdas/branches/short-circuit and read-only r+ cannot claim external changes")

            for label, mode in (("keyword", "mode='w'"), ("read-write", "'r+'")):
                session = "open-" + label
                start(session); material(session)
                code = "\n".join(f"with open({name!r}, {mode}) as f:\n    f.write({content!r})"
                                 for name, content in (("handoff-current.md", dashboard.read_text()+f"\n{label}\n"),
                                                       ("handoffs/active/task.md", detail.read_text()+f"\n{label}\n")))
                subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True)
                hook(session, "PostToolUse", "bash", {"command": "python3 - <<'PY'\n" + code + "\nPY"})
                assert stop(session) == {}
            print("  PASS open(mode='w') and open(..., 'r+') recognize direct write targets")

            start("one"); material("one")
            dashboard.write_text(dashboard.read_text()+"\nOnly dashboard\n")
            hook("one", "PostToolUse", "bash", {"command": "Path('handoff-current.md').write_text('new')"})
            assert stop("one").get("decision") == "block"
            print("  PASS updating only one handoff still blocks")

            # 已在运行的旧会话：先有一次普通工具调用即可建立基线，不清掉旧 pending。
            hook("legacy", "PostToolUse", "bash", {"command": "pwd"})
            material("legacy"); python_handoff("legacy", "\nLegacy session\n")
            assert stop("legacy") == {}
            print("  PASS existing session initializes observation without a restart")

            start("new-detail"); material("new-detail")
            new_detail = "handoffs/active/new-task.md"
            code = f"from pathlib import Path; p=Path('handoff-current.md'); p.write_text(p.read_text()+{chr(10)+'- `'+new_detail+'`'+chr(10)!r}); Path({new_detail!r}).write_text('# New task')"
            subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True)
            hook("new-detail", "PostToolUse", "bash", {"command": "python3 -c " + shlex.quote(code)})
            assert stop("new-detail") == {}
            print("  PASS a new active handoff is recognized with the dashboard update")

            start("recursive"); material("recursive")
            assert "systemMessage" in hook("recursive", "Stop", stop_active=True)
            print("  PASS recursive Stop warns rather than blocking again")

            start("budget"); material("budget")
            for attempt in range(3):
                dashboard.write_text(dashboard.read_text()+f"\nUnrelated external change {attempt}\n")
                assert stop("budget").get("decision") == "block"
            dashboard.write_text(dashboard.read_text()+"\nAnother external change\n")
            assert "systemMessage" in stop("budget")
            print("  PASS even changing unresolved conditions cannot exceed three blocks")
        finally:
            # 项目状态目录可能带根路径摘要；让待测版本自己计算，不依赖其实现。
            command = "import sys;sys.path.insert(0,'scripts');import protocol,protocol_guard as g; p=protocol.load(); print(g.temp_root(p.name,p.root))"
            state_root = subprocess.check_output([sys.executable, "-c", command], cwd=root, env=env, text=True).strip()
            shutil.rmtree(state_root, ignore_errors=True)
    print("PASS:", source)


if __name__ == "__main__":
    run(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
