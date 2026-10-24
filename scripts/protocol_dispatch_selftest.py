# -*- coding: utf-8 -*-
"""自检 / self-check for protocol_dispatch: `python3 scripts/protocol_dispatch_selftest.py`.

覆盖全局 hook 路径上真正会出错的三件事 / covers the three things that actually break on
the global hook path:

1. 从**别的**工作目录出发也能按会话 cwd 找到项目根 / the project root is found from the
   session cwd even when the hook process starts somewhere else;
2. 没有 `.agent-workspace.json` 的项目被干净跳过（D:\\投资工作站 这类）/ a workspace
   without the config is skipped silently and can never be pulled into the protocol;
3. stdin 原样转交、子进程 stdout 原样转发（SessionStart 注入、Stop 拦截）/ stdin is
   forwarded verbatim and the child's verdict reaches the host unchanged.

每个用例都用子进程跑真实入口，只用临时项目；退出码 0 表示全部通过。
Everything runs the real entry point in a subprocess against throwaway projects.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import protocol_dispatch as dispatch  # noqa: E402
import protocol_guard as guard  # noqa: E402

RUNTIME_SCRIPTS = ("protocol.py", "protocol_context.py", "protocol_guard.py", "protocol_handoff_confirm.py")
CONFIG = {
    "version": 1,
    "name": "dispatch-selftest",
    "marker": ".agent-workspace-managed",
    "dashboard": {"file": "handoff-current.md", "limitBytes": 5120},
    "handoffs": {"active": "handoffs/active/", "archive": "handoffs/archive/"},
    "memory": {"index": "memory/INDEX.md"},
    "writePolicy": {
        "material": {"rootFiles": ["agents.md"], "prefixes": ["problems/", "scripts/"]},
        "ignored": {"prefixes": ["tmp/"], "contains": ["/raw/"]},
        "advisory": {"prefixes": ["scratch/"]},
        "ordinary": {"rootFiles": [".gitignore"]},
    },
}
DASHBOARD = (
    "## Active/waiting workstreams\n\n"
    "- `handoffs/active/selftest-workstream.md` — Status: In progress\n\n"
    "## Recent\n\n- nothing\n"
)
SESSION = "dispatch-selftest-session"
# 一个真实存在、但绝不属于任何已接入项目的 Windows 路径 / a real Windows path that is no
# adopted project: the drive form the host may put in `cwd`.
WINDOWS_CWD = "C:\\Windows\\System32"
FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   " + label)
        return
    FAILURES.append(label)
    print("  FAIL " + label + " " + detail)


def call(payload: dict, cwd: str) -> subprocess.CompletedProcess:
    """像宿主那样调用入口 / invoke the entry point the way the host does."""
    return subprocess.run(
        [sys.executable, str(HERE / "protocol_dispatch.py")],
        input=json.dumps(payload).encode("utf-8"),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def log_tail() -> str:
    path = Path(tempfile.gettempdir()) / dispatch.LOG_NAME
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def make_project(base: Path, name: str) -> Path:
    project = base / name
    (project / "scripts").mkdir(parents=True)
    (project / "handoffs" / "active").mkdir(parents=True)
    (project / "problems").mkdir(parents=True)
    (project / ".agent-workspace.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (project / "handoff-current.md").write_text(DASHBOARD, encoding="utf-8")
    (project / "handoffs" / "active" / "selftest-workstream.md").write_text("# workstream\n", encoding="utf-8")
    for script in RUNTIME_SCRIPTS:
        shutil.copy(HERE / script, project / "scripts" / script)
    return project


def run() -> int:
    base = Path(tempfile.mkdtemp(prefix="protocol-dispatch-selftest-")).resolve()
    neutral = Path(tempfile.mkdtemp(prefix="protocol-dispatch-neutral-")).resolve()
    project = make_project(base, "adopted")
    bare = base / "bare"
    bare.mkdir()
    (bare / ".agent-workspace.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    state_dir = guard.temp_root(CONFIG["name"], project)

    try:
        print("find_project: 按会话 cwd 定位项目根 / locate the root from the session cwd")
        nested = project / "handoffs" / "active"
        check("a nested workdir still resolves to the project root",
              dispatch.find_project([str(nested)]) == project, str(dispatch.find_project([str(nested)])))
        check("an unrelated workdir resolves to nothing",
              dispatch.find_project([str(neutral)]) is None, str(dispatch.find_project([str(neutral)])))
        check("mnt_form maps a Windows drive path", dispatch.mnt_form("D:\\投资工作站") == "/mnt/d/投资工作站",
              str(dispatch.mnt_form("D:\\投资工作站")))

        print("WSL UNC：中性进程目录按会话工作区分发 / session UNC from a neutral process cwd")
        distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
        unc = "\\\\wsl.localhost\\" + distro + str(project).replace("/", "\\")
        with_distro = os.environ.get("WSL_DISTRO_NAME")
        os.environ["WSL_DISTRO_NAME"] = distro
        try:
            check("WSL localhost UNC maps to the current distribution", dispatch.mnt_form(unc) == str(project))
            check("WSL dollar UNC also maps", dispatch.mnt_form(unc.replace("wsl.localhost", "wsl$")) == str(project))
            foreign = unc.replace(distro, "foreign-distribution", 1)
            check("a foreign distribution cannot alias this filesystem", dispatch.mnt_form(foreign) is None)
            result = call({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup", "cwd": unc}, str(neutral))
            context = json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext", "") if result.stdout else ""
            check("UNC SessionStart injects from a neutral process cwd", result.returncode == 0 and "Active/waiting workstreams" in context)
            result = call({"session_id": SESSION, "hook_event_name": "SessionStart", "cwd": foreign}, str(neutral))
            check("a foreign UNC session is skipped", result.returncode == 0 and result.stdout == b"")
        finally:
            if with_distro is None:
                os.environ.pop("WSL_DISTRO_NAME", None)
            else:
                os.environ["WSL_DISTRO_NAME"] = with_distro

        print("真实入口：会话在已接入项目里 / the real entry point inside an adopted project")
        before = log_tail()
        result = call({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup", "cwd": str(project)}, str(neutral))
        output = result.stdout.decode("utf-8", "replace").strip()
        parsed = json.loads(output) if output else {}
        context = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
        check("SessionStart injects the dashboard even though the hook process starts elsewhere",
              result.returncode == 0 and "Active/waiting workstreams" in context, output[:200])
        check("the dispatch is logged as evidence", "SessionStart ran protocol_context.py" in log_tail()[len(before):],
              log_tail()[len(before):][-200:])

        result = call({"session_id": SESSION, "hook_event_name": "PostToolUse", "cwd": str(project),
                       "tool_name": "read", "tool_input": {"file_path": "problems/0001-x.md"}, "tool_response": "ok"}, str(neutral))
        check("a read-only tool stays quiet", result.returncode == 0 and result.stdout == b"", result.stdout[:200])

        result = call({"session_id": SESSION, "hook_event_name": "PostToolUse", "cwd": str(project),
                       "tool_name": "write", "tool_input": {"file_path": "problems/0002-y.md"}, "tool_response": "diff"}, str(neutral))
        check("a material write stays quiet on the post-tool path", result.returncode == 0 and result.stdout == b"", result.stdout[:200])

        result = call({"session_id": SESSION, "hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(project)}, str(neutral))
        output = result.stdout.decode("utf-8", "replace").strip()
        parsed = json.loads(output) if output else {}
        check("Stop blocks because the handoff was not updated", parsed.get("decision") == "block", output[:300])

        print("Windows 形状的 payload cwd / a Windows-shaped payload cwd")
        # 宿主可能把会话 cwd 报成 Windows 路径（D:\\... 或 C:\\...）；只要 hook 进程自己的
        # cwd 落在项目里，发现流程就不能被这个字段带偏。
        # The host may report the session cwd as a Windows path; discovery must still work
        # from the hook process's own cwd.
        before = log_tail()
        result = call({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup",
                       "cwd": WINDOWS_CWD}, str(project))
        output = result.stdout.decode("utf-8", "replace").strip()
        parsed = json.loads(output) if output else {}
        context = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
        check("a Windows payload cwd does not break discovery when the process cwd is the project",
              result.returncode == 0 and "Active/waiting workstreams" in context, output[:200])
        check("the run is still logged", "SessionStart ran protocol_context.py" in log_tail()[len(before):],
              log_tail()[len(before):][-200:])

        before = log_tail()
        result = call({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup",
                       "cwd": WINDOWS_CWD}, str(neutral))
        check("a Windows payload cwd outside every adopted project is skipped",
              result.returncode == 0 and result.stdout == b""
              and "skip: no .agent-workspace.json" in log_tail()[len(before):],
              result.stdout[:200])

        print("真实入口：会话不在任何已接入项目里 / the real entry point outside every adopted project")
        before = log_tail()
        result = call({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup", "cwd": str(neutral)}, str(neutral))
        check("an unrelated workspace produces no output and exit 0",
              result.returncode == 0 and result.stdout == b"" and result.stderr == b"", result.stdout[:200])
        check("the skip is logged", "skip: no .agent-workspace.json" in log_tail()[len(before):], log_tail()[len(before):][-200:])

        before = log_tail()
        result = call({"session_id": SESSION, "hook_event_name": "Stop", "cwd": str(bare)}, str(neutral))
        check("a config without scripts/ is skipped, not crashed",
              result.returncode == 0 and result.stdout == b"" and "has no scripts/protocol_guard.py" in log_tail()[len(before):],
              result.stderr.decode("utf-8", "replace")[:200])

        print("")
        if FAILURES:
            print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
            return 1
        print("all checks passed")
        return 0
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(neutral, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
