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
        # 引号内的空格是路径的一部分，不能被拆成多个词 / spaces inside quotes belong to the path.
        for command, expected in (
            ('cp draft.md "problems/Chapter 2 draft.md"', ["problems/chapter 2 draft.md"]),
            ("cp draft.md 'problems/Chapter 3 draft.md'", ["problems/chapter 3 draft.md"]),
            ('mv "problems/old name.md" "problems/new name.md"', ["problems/old name.md", "problems/new name.md"]),
        ):
            item = payload("bash", {"command": command}, "")
            check("quoted path keeps its spaces: " + command,
                  guard.classify_write(proto, item)["material"] == expected, str(guard.classify_write(proto, item)))
        # 复合命令的每一段都要单独判定，前一段只读不代表后一段没写 / every segment is judged.
        for command in ("ls problems/ | head -3 && cp draft.md problems/0006-a.md",
                        "echo start; printf hi > problems/0007-b.md",
                        "true || touch problems/0008-c.md"):
            item = payload("bash", {"command": command}, "")
            check("compound command is split: " + command,
                  guard.classify_write(proto, item)["material"], str(guard.classify_write(proto, item)))
        # 引号里的 && 只是文本，不是命令分隔符 / a quoted && is text, not a separator.
        item = payload("bash", {"command": 'printf "a && b" > problems/0009-d.md'}, "")
        check("quoted && does not become a separator",
              guard.classify_write(proto, item)["material"] == ["problems/0009-d.md"], str(guard.classify_write(proto, item)))

        print("裸文件名也是目标 / a bare filename is still a target")
        # 真实触发：交接文件常驻项目根（`handoff-current.md`、`README.md`），名字里没有
        # 斜杠；只收「像路径的词」会全部漏掉，于是更新了 dashboard 却不算数，Stop 永远拦。
        for command, kind, expected in (
            ("sed -i 's/a/b/' handoff-current.md", "dashboard", ["handoff-current.md"]),
            ("cp draft.md handoff-current.md", "dashboard", ["handoff-current.md"]),
            ("echo x >> handoff-current.md", "dashboard", ["handoff-current.md"]),
            ("tee handoff-current.md", "dashboard", ["handoff-current.md"]),
            ("touch handoff-current.md", "dashboard", ["handoff-current.md"]),
            ("mv old.md agents.md", "material", ["agents.md"]),
        ):
            item = payload("bash", {"command": command}, "")
            got = guard.classify_write(proto, item)
            check("bare name is a target: " + command, got[kind] == expected, str(got))
        # sed 的第一个非选项参数是脚本，不是文件。
        item = payload("bash", {"command": "sed -i 's/a/b/' handoff-current.md"}, "")
        check("the sed script is not a path",
              "s/a/b/" not in str(guard.classify_write(proto, item)), str(guard.classify_write(proto, item)))
        # 只读的 sed（无 -i）不算写入。
        item = payload("bash", {"command": "sed -n '1,3p' handoff-current.md"}, "")
        check("sed without -i is a read", not any(guard.classify_write(proto, item).values()),
              str(guard.classify_write(proto, item)))
        # 写进 /dev/null、NUL 之类的黑洞不是改项目文件。
        for command in ("cat a.md | tee /dev/null > /dev/null", "cmd > /dev/null 2>&1",
                        "cmd 2>/dev/null", "tee /dev/null", "echo hi > NUL"):
            item = payload("bash", {"command": command}, "")
            got = guard.classify_write(proto, item)
            check("a bit bucket is not a write: " + command, not any(got.values()), str(got))

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

        print("python_write_paths: 直线语句里的路径变量 / a straight-line path variable survives")
        def heredoc(code: str) -> str:
            return "python3 - <<'PY'\n" + code + "\nPY\n"
        # assert/pass/def/class 都不重新绑定变量，不得把已绑定的路径清掉。
        for label, middle in (
            ("plain", ""),
            ("after assert", "assert a in s\n"),
            ("after pass", "pass\n"),
            ("after def", "def helper():\n    return 1\n"),
            ("after class", "class Box:\n    pass\n"),
        ):
            code = 'P = "problems/0010-e.md"\ns = "x"\na = "x"\n' + middle + 'open(P, "w").write(s)'
            got = guard.python_write_paths(heredoc(code))
            check(f"path variable survives {label}", got == ["problems/0010-e.md"] * 2, str(got))
        # 真正的控制流仍须保守：if 可能重新绑定，路径不得被追认。
        code = 'P = "problems/0011-f.md"\nif a:\n    pass\nopen(P, "w").write("x")'
        check("a real branch still clears the path", not guard.python_write_paths(heredoc(code)),
              str(guard.python_write_paths(heredoc(code))))

        print("python_write_paths: pathlib 写法 / pathlib spellings")
        # 真实触发：用 `pathlib.Path(...).write_text(...)` 更新交接时守卫认不出目标，
        # 于是「改了交接」这一步永远不算数，回合怎么都收不了尾。
        for label, code in (
            ("a bare Path target is found",
             'p = Path("handoffs/active/y.md")\np.write_text("x")'),
            ("a pathlib.Path target is found",
             'p = pathlib.Path("handoffs/active/y.md")\np.write_text("x")'),
            ("a pathlib.Path call chain is found",
             'pathlib.Path("handoffs/active/y.md").write_text("x")'),
            ("a resolved Path target is found",
             'p = pathlib.Path("handoffs/active/y.md").resolve()\np.write_text("x")'),
            ("a joined Path target is found",
             'p = pathlib.Path("handoffs") / "active" / "y.md"\np.write_text("x")'),
        ):
            got = guard.python_write_paths(heredoc(code))
            check(label, got == ["handoffs/active/y.md"], str(got))
        # 只读 pathlib 用法不得算写入。
        read_only = 'p = pathlib.Path("handoffs/active/y.md")\nprint(p.read_text())'
        check("reading through pathlib is not a write", not guard.python_write_paths(heredoc(read_only)),
              str(guard.python_write_paths(heredoc(read_only))))

        print("内联 python 的写入要进 material / an inline python write is material")
        # 真实触发：整轮都在用 `python3 - <<'PY' … p.write_text(…)` 改文件，而 shell 分支
        # 只按命令词判断，于是改了文件却不算数，交接更新后仍被判为没改。
        for label, command, expected in (
            ("an inline python write is material",
             "python3 - <<'PY'\nfrom pathlib import Path\n"
             "p = pathlib.Path('problems/0020-p.md')\np.write_text('x')\nPY",
             ["problems/0020-p.md"]),
            ("an inline python call chain is material",
             "python3 - <<'PY'\nimport pathlib\n"
             "pathlib.Path('problems/0021-q.md').write_text('x')\nPY",
             ["problems/0021-q.md"]),
        ):
            got = guard.classify_write(proto, payload("bash", {"command": command}, "ok"))
            check(label, got["material"] == expected, str(got))
        # 只读的内联 python 不得进 material。
        quiet = guard.classify_write(proto, payload(
            "bash", {"command": "python3 - <<'PY'\nfrom pathlib import Path\n"
                                "p = pathlib.Path('problems/0022-r.md')\nprint(p.read_text())\nPY"}, "ok"))
        check("an inline python read is not material", not any(quiet.values()), str(quiet))

        print("heredoc 正文与箭头不是写入 / a heredoc body and an arrow are not writes")
        # 真实触发：用 heredoc 写一段提到路径的说明，守卫把正文当命令，之后怎么更新交接都清不掉。
        for label, command in (
            ("a heredoc body mentioning a write is not a write",
             "python3 - <<'PY'\ntests = ['grep -c tee scripts/protocol_guard.py']\nPY"),
            ("a heredoc body mentioning cp is not a write",
             "cat <<'EOF'\nnext: cp a.md papers/b.md\nEOF"),
            ("an arrow is not a redirect", 'echo "path ->"'),
        ):
            got = guard.classify_write(proto, payload("bash", {"command": command}, "ok"))
            check(label, not any(got.values()), str(got))
        # 但真正执行的 heredoc 写入仍须拦住。
        real = guard.classify_write(
            proto, payload("bash", {"command": "cat > scripts/x.py <<'EOF'\nprint(1)\nEOF"}, "ok"))
        check("a heredoc that really writes still counts", real["material"] == ["scripts/x.py"], str(real))

        print("只读命令里提到的写入词不是写入 / a write word inside a read command is not a write")
        # 真实触发：`grep -n "cp " scripts/x.py` 被判成改了 scripts/x.py，交接怎么更新都清不掉，
        # 回合永远收不了尾。只有命令位置上的动词才算写入。
        for label, command in (
            ("grep mentioning cp", 'grep -n "cp " scripts/protocol_guard.py'),
            ("grep mentioning tee", 'grep -n "tee " scripts/protocol_guard.py'),
            ("grep mentioning touch", 'grep -rn "touch " scripts/'),
            ("grep mentioning a redirect", 'grep -n "printf .* > " scripts/x.py'),
            ("grep mentioning a compound command", 'grep -n "a && cp b" handoffs/active/x.md'),
            ("2>&1 is not a redirect", "ls -la 2>&1 | head -3"),
        ):
            got = guard.classify_write(proto, payload("bash", {"command": command}, "ok"))
            check(label, not any(got.values()), str(got))
        # 但命令位置上的写入仍须拦住，包括前缀与嵌套写法。
        for label, command, expected in (
            ("sudo cp still writes", "sudo cp a.md problems/0012-g.md", ["problems/0012-g.md"]),
            ("sed -i still writes", "sed -i s/a/b/ problems/0013-h.md", ["problems/0013-h.md"]),
            ("a nested shell still writes", 'bash -c "cp a.md problems/0014-i.md"', ["problems/0014-i.md"]),
            ("rm still writes", "rm problems/0015-j.md", ["problems/0015-j.md"]),
            ("mkdir still writes", "mkdir problems/0016-k", ["problems/0016-k"]),
            ("xargs still writes", "xargs rm problems/0017-l.md", ["problems/0017-l.md"]),
            ("find -exec still writes", "find . -exec rm problems/0018-m.md \\;", ["problems/0018-m.md"]),
            ("dd of= still writes", "dd if=/dev/zero of=problems/0019-n.md", ["problems/0019-n.md"]),
        ):
            got = guard.classify_write(proto, payload("bash", {"command": command}, "ok"))
            check(label, got["material"] == expected, str(got))

        print("temp_root: 同名克隆不得共享状态 / same-named clones stay isolated")
        check("the project root hashes into the state directory",
              guard.temp_root(proto.name, Path("/a/x")) != guard.temp_root(proto.name, Path("/b/x")))

        print("Stop: 同一未解决区间只拦一次 / one bounded repair attempt")
        state.unlink(missing_ok=True)
        decisions, diagnostics = [], []
        for index in range(4):
            (workspace / "handoff-current.md").write_text(DANGLING_DASHBOARD + "x" * index, encoding="utf-8")
            # 后续 material 写入产生新 token，也不能重置未解决区间的拦截额度。
            guard.handle_post_tool(proto, root, payload("write", {"file_path": "problems/pending.md"}), state)
            buffer, diagnostic = io.StringIO(), io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(diagnostic):
                guard.handle_stop(proto, state, {"hook_event_name": "Stop", "stop_hook_active": False})
            decisions.append(json.loads(buffer.getvalue() or "{}"))
            diagnostics.append(diagnostic.getvalue())
        check("the repair budget is exactly one", guard.STOP_BLOCK_LIMIT == 1)
        check("only the first stop blocks", [item.get("decision") for item in decisions] == ["block", None, None, None])
        check("the block tells the agent to finish without debugging the guard",
              "at most one bounded handoff correction" in decisions[0].get("reason", "")
              and "Do not keep rewriting handoffs or debug/modify the guard" in decisions[0].get("reason", ""))
        check("the next stop warns without pretending the handoff passed",
              "systemMessage" in decisions[1] and "handoff is still unresolved" in diagnostics[1])
        check("further file changes cannot repeat the warning", decisions[2:] == [{}, {}] and diagnostics[2:] == ["", ""])
        check("repeated material writes retain pending but spend only one block",
              guard.load_state(state).get("material_pending") and guard.load_state(state).get("stop_blocks") == 1)

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
