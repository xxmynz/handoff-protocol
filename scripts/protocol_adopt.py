#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采用/初始化协议工作区 —— 傻瓜式一条命令 / adopt or refresh a protocol workspace.

**放轻松，只有两件事要记：**
  1. 在终端里跑这一条命令（不写参数 = 处理你当前所在的项目）：
         python3 scripts/protocol_adopt.py
  2. 先用 --dry-run 预览；交互终端默认询问，非交互环境可能直接写入。

用法 / Usage:
    python3 scripts/protocol_adopt.py                      # 处理当前目录（最常用）
    python3 scripts/protocol_adopt.py --target ~/projects/my-project        # 处理指定项目
    python3 scripts/protocol_adopt.py --target 'D:\\投资工作站'           # Windows 侧项目也行
    python3 scripts/protocol_adopt.py --dry-run             # 只看计划，绝不写文件
    python3 scripts/protocol_adopt.py --yes                 # 不询问，直接执行（CI 用）
    python3 scripts/protocol_adopt.py --check-sync          # 只检查哪些项目的脚本副本落后模板
    python3 scripts/protocol_adopt.py --check-sync --sync-targets ~/projects/a,~/projects/b

行为 / Behaviour:
  * 只创建**缺失**的骨架文件；已存在的 AGENTS.md / handoff-current.md / memory/ 一律不动。
  * .agent-workspace.json 已存在 → 保留项目设置，仅清理明确列出的废弃键；不存在 → 从模板生成。
  * 协议脚本总是同步到模板版本（它们是协议本体，不是项目内容）。
  * 幂等：已采用的仓库再跑一次只会同步脚本，其余全部 "kept"。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TEMPLATE_ROOT = HERE.parent
TEMPLATES = TEMPLATE_ROOT / "templates"
CONFIG_NAME = ".agent-workspace.json"
PLACEHOLDER = "{{PROJECT_NAME}}"
TEXT_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".txt"}

PROTOCOL_SCRIPTS = (
    "protocol.py",
    "protocol_guard.py",
    "protocol_handoff_confirm.py",
    "protocol_context.py",
    "protocol_validate.py",
    "protocol_mcp_server.py",
    "protocol_adopt.py",
)
SKELETON_DIRS = ("handoffs/active", "handoffs/archive", "memory/stable", "memory/completed", "skills")

CREATE_FILES = "created files"
UPDATE_FILES = "updated files"
KEEP_FILES = "kept files"
PRUNE_KEYS = "pruned legacy keys"

# 协议自己曾经发过、后来删掉的配置键。adopt 是配置 schema 的迁移点：运行时顺手清掉，
# 并且**明确打印出来**，不静默改用户配置。
REMOVED_CONFIG_KEYS = ("mcp",)


# --------------------------------------------------------------------------- #
# 路径：把宿主给出的 Windows / UNC 写法翻译成**当前系统**里真实存在的路径
# --------------------------------------------------------------------------- #

class TargetPathError(ValueError):
    """路径写法与当前系统对不上，不能靠猜 / the spelling does not belong to this host."""


def host_is_windows() -> bool:
    """解释器是 Windows 原生 Python（不是 WSL）/ native Windows Python, not WSL."""
    return os.name == "nt"


def in_wsl() -> bool:
    """解释器跑在 WSL 里 / True only inside WSL.

    Linux 容器或纯 Linux 返回 False：那里没有 /mnt/d/...，翻译过去等于编一个不存在的路径。
    """
    if host_is_windows():
        return False
    if os.environ.get("WSL_DISTRO_NAME", "").strip():
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


WINDOWS_FORM_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|\\\\wsl(?:\.localhost|\$)\\[^\\]+\\|//wsl(?:\.localhost|\$)/[^/]+/)",
    re.IGNORECASE,
)


def translate_target(value: str) -> Path:
    """把宿主给出的 Windows / UNC 写法变成本机真实路径。

    - Windows 原生：盘符和 UNC 本身就是本地路径，原样使用；
    - WSL：D:\\投资工作站 → /mnt/d/投资工作站，\\\\wsl.localhost\\Ubuntu\\home\\x → /home/x；
    - 其他 Linux：无法翻译，报错而不是编出一个 /mnt/d/... 路径。
    """
    text = value.strip().strip("\"'")
    if not WINDOWS_FORM_RE.match(text) or host_is_windows():
        return Path(text).expanduser()
    if not in_wsl():
        raise TargetPathError(
            "这是 Windows 路径写法，但当前不在 WSL 里，无法翻译：" + text + "\n"
            "  在 Windows 上请用 Windows 版 Python 运行；在 Linux 上请传本机绝对路径。"
        )
    unc = re.match(r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\(.*)$", text, re.IGNORECASE)
    if unc:
        return Path("/" + unc.group(2).replace("\\", "/"))
    drive = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    return Path("/mnt/{}/{}".format(drive.group(1).lower(), drive.group(2).replace("\\", "/")))


def find_templates() -> Path | None:
    """找到模板骨架在哪 / locate the template skeleton bundle.

    顺序：脚本旁边 → 环境变量 AGENT_PROTOCOL_TEMPLATE → 模板仓库常见位置 → ~/projects/* 。
    这样即使你在项目里跑的是脚本副本，也能自动找到骨架，不用记路径。
    """
    if (TEMPLATES / CONFIG_NAME).exists():
        return TEMPLATES

    candidates: list[Path] = []
    env = os.environ.get("AGENT_PROTOCOL_TEMPLATE", "").strip()
    if env:
        candidates.append(Path(env).expanduser() / "templates")
    for base in (HERE.parent, HERE.parent.parent):
        candidates.append(base / "agent-protocol-template" / "templates")
    projects = Path.home() / "projects"
    if projects.is_dir():
        try:
            candidates.extend(sorted(child / "templates" for child in projects.iterdir() if child.is_dir()))
        except OSError:
            pass
    for candidate in candidates:
        if (candidate / CONFIG_NAME).exists():
            return candidate
    return None


def render(source: Path, name: str) -> bytes:
    data = source.read_bytes()
    if source.suffix in TEXT_SUFFIXES:
        return data.decode("utf-8").replace(PLACEHOLDER, name).encode("utf-8")
    return data


# --------------------------------------------------------------------------- #
# 计划：先算清楚要做什么，再决定写不写
# --------------------------------------------------------------------------- #

def declared_marker(target: Path) -> str | None:
    """读目标项目自己的 marker 名 / the marker file this project declares."""
    try:
        raw = json.loads((target / CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    marker = raw.get("marker")
    return str(marker) if marker else None


def legacy_keys(target: Path) -> list[str]:
    """配置里残留的、协议已经删掉的键 / keys the schema no longer defines."""
    try:
        raw = json.loads((target / CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    return [key for key in REMOVED_CONFIG_KEYS if key in raw]


def build_plan(target: Path, templates: Path, name: str, adopted: bool) -> dict[str, list[str]]:
    plan: dict[str, list[str]] = {
        CREATE_FILES: [],
        UPDATE_FILES: [],
        KEEP_FILES: [],
        PRUNE_KEYS: [],
    }

    # 1) 配置：存在就保留（只清掉已删除的旧键），不存在才生成。
    if adopted:
        plan[KEEP_FILES].append(CONFIG_NAME)
        plan[PRUNE_KEYS].extend(legacy_keys(target))
    else:
        plan[CREATE_FILES].append(CONFIG_NAME)

    if adopted:
        # 已采用的项目：绝不注入模板正文/示例，只保证 marker 在，之后就只同步脚本。
        marker = declared_marker(target) or ".agent-workspace-managed"
        bucket = KEEP_FILES if (target / marker).exists() else CREATE_FILES
        plan[bucket].append(marker)
    else:
        # 项目已经自己维护 dashboard（手写/仿制过协议）时，不要把模板里的示例 workstream
        # 塞进它真实的 handoffs/active/，否则会被当成一个 active workstream 列出来。
        has_own_dashboard = (target / "handoff-current.md").exists()
        # 2) 骨架：逐文件只补缺失。
        for source in sorted(templates.rglob("*")):
            if source.is_dir() or "__pycache__" in source.parts:
                continue
            relative = source.relative_to(templates).as_posix()
            if relative == CONFIG_NAME:
                continue
            if has_own_dashboard and source.name.startswith("example"):
                plan[KEEP_FILES].append(relative + "  (skipped: project has its own dashboard)")
                continue
            bucket = KEEP_FILES if (target / relative).exists() else CREATE_FILES
            plan[bucket].append(relative)

        # 3) 空目录骨架（git 不跟踪空目录，这里也要建出来）。
        for relative in SKELETON_DIRS:
            if not (target / relative).exists():
                plan[CREATE_FILES].append(relative + "/")

    # 4) 协议脚本：总是同步到模板版本。
    for script in PROTOCOL_SCRIPTS:
        source = HERE / script
        if not source.exists():
            print(f"warning: {script} missing next to this script", file=sys.stderr)
            continue
        destination = target / "scripts" / script
        if not destination.exists():
            plan[CREATE_FILES].append(f"scripts/{script}")
        elif destination.read_bytes() == source.read_bytes():
            plan[KEEP_FILES].append(f"scripts/{script}")
        else:
            plan[UPDATE_FILES].append(f"scripts/{script}")
    return plan


def apply_plan(
    target: Path, templates: Path, name: str, plan: dict[str, list[str]], adopted: bool
) -> None:
    if CONFIG_NAME in plan[CREATE_FILES]:
        config = json.loads((templates / CONFIG_NAME).read_text(encoding="utf-8"))
        config["name"] = name
        config.pop("templates", None)      # 模板仓库自身的键
        config["policy"] = []              # 示例策略不进真实项目
        (target / CONFIG_NAME).write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    for item in plan[CREATE_FILES]:
        destination = target / item
        if item.endswith("/"):
            destination.mkdir(parents=True, exist_ok=True)
        elif item.startswith("scripts/"):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((HERE / Path(item).name).read_bytes())
        elif item != CONFIG_NAME and (templates / item).exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(render(templates / item, name))

    # 配置迁移：只删协议自己删掉的键，其余内容与键序原样保留。
    if plan[PRUNE_KEYS]:
        config_path = target / CONFIG_NAME
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        for key in plan[PRUNE_KEYS]:
            raw.pop(key, None)
        config_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # 已采用项目若缺 marker（零字节占位文件），补一个空的即可。
    if adopted:
        marker = declared_marker(target) or ".agent-workspace-managed"
        path = target / marker
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    for item in plan[UPDATE_FILES]:
        destination = target / item
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((HERE / Path(item).name).read_bytes())


def print_plan(target: Path, name: str, plan: dict[str, list[str]], adopted: bool) -> None:
    creates = len(plan[CREATE_FILES])
    updates = len(plan[UPDATE_FILES])
    print("=" * 68)
    print("协议接入计划 / protocol adoption plan")
    print("=" * 68)
    print(f"  项目目录 : {target}")
    print(f"  项目名   : {name}")
    print(f"  当前状态 : {'已采用，本次在原位刷新' if adopted else '尚未采用，本次新建骨架'}")
    if adopted:
        print("  本次范围 : 只检查配置 / marker / 协议脚本；")
        print("             AGENTS.md、handoff、memory、handoffs 一律不碰（未列在下面 ≠ 不存在）")
    print("-" * 68)
    print(f"  将新建 {creates} 项   将更新 {updates} 项   保持不动 {len(plan[KEEP_FILES])} 项")
    if plan[PRUNE_KEYS]:
        print(f"  将清理 {len(plan[PRUNE_KEYS])} 个已废弃的旧配置键: {', '.join(plan[PRUNE_KEYS])}")
    for label in (CREATE_FILES, UPDATE_FILES):
        if plan[label]:
            print(f"\n  [{label}]")
            for item in sorted(plan[label]):
                print(f"    + {item}")
    if plan[KEEP_FILES]:
        print(f"\n  [{KEEP_FILES}]（一个字节都不会动）")
        for item in sorted(plan[KEEP_FILES]):
            print(f"    = {item}")
    print("-" * 68)
    print("  安全保证：已存在的文件绝不覆盖；只有上面带 + 的项目会被写。")


def check_sync(targets: list[Path], source: Path) -> dict[str, Any]:
    """检查各项目的协议脚本副本是否还等于模板版本 / report script drift per project.

    协议脚本是**副本分发**的：每个接入了协议的项目都带一份完整拷贝，没有自动升级通道。
    改了模板却忘了同步，就会出现"模板已修、项目里还是旧代码"的静默不一致——那是本协议最
    容易出的运营错误，所以这里给它一个显式出口，退出码 0/1 可直接进自动化。
    """
    report: dict[str, Any] = {"source": str(source), "projects": [], "in_sync": True}
    for target in targets:
        adopted = (target / CONFIG_NAME).exists()
        entry: dict[str, Any] = {
            "target": str(target),
            "adopted": adopted,
            "identical": [],
            "drift": [],
            "missing": [],
        }
        for script in PROTOCOL_SCRIPTS:
            origin = source / script
            copy = target / "scripts" / script
            if not origin.exists():
                continue
            if not copy.exists():
                # 只有已接入的项目才谈得上"副本缺失"；没接入的项目本来就没有副本。
                if adopted:
                    entry["missing"].append(script)
            elif copy.read_bytes() == origin.read_bytes():
                entry["identical"].append(script)
            else:
                entry["drift"].append(script)
        entry["checked"] = adopted
        entry["ok"] = not entry["drift"] and not entry["missing"]
        if adopted and not entry["ok"]:
            report["in_sync"] = False
        report["projects"].append(entry)
    return report


def print_sync_report(report: dict[str, Any]) -> None:
    print("=" * 68)
    print("协议脚本副本同步检查 / protocol script copy drift")
    print("=" * 68)
    print(f"  模板版本 : {report['source']}")
    print("-" * 68)
    for entry in report["projects"]:
        if not entry["checked"]:
            print(f"\n  {entry['target']}")
            print("    - 还没接入协议（没有配置文件），跳过")
            continue
        state = "一致" if entry["ok"] else "落后/缺失"
        print(f"\n  {entry['target']}  [{state}]")
        print(f"    一致 {len(entry['identical'])} 项")
        for script in entry["drift"]:
            print(f"    ! 落后模板: scripts/{script}")
        for script in entry["missing"]:
            print(f"    ! 缺少: scripts/{script}")
        if not entry["ok"]:
            adopt_script = Path(report["source"]) / "protocol_adopt.py"
            print(f"    → 修复: python3 {adopt_script} --target {entry['target']}")
    print("-" * 68)
    if report["in_sync"]:
        print("结果：所有已接入项目都与模板一致。")
    else:
        print("结果：有项目落后于模板（上面的 ! 行）；跑上面给出的 adopt 命令即可同步。")


def next_steps(target: Path) -> None:
    quoted = shlex.quote(str(target))
    print("\n下一步（复制粘贴，一行一行来）：")
    print(f"  1) cd {quoted} && python3 scripts/protocol_validate.py")
    print("  2) 打开 AGENTS.md 和 AGENT-BOOTSTRAP.md，填这个项目的真实规则")
    print("  3) 打开 .agent-workspace.json，把 writePolicy.material 改成这个仓库真实的重要目录")
    print("     完整说明：docs/quickstart.md")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--target", default=None, help="目标项目目录，默认当前目录 / destination (default: .)"
    )
    parser.add_argument("--name", help="项目名，默认取目录名 / project name (default: folder name)")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    parser.add_argument("--yes", "-y", action="store_true", help="不询问，直接执行")
    parser.add_argument(
        "--template", help="模板仓库位置，默认是本脚本所在仓库 / template checkout path"
    )
    parser.add_argument(
        "--check-sync",
        action="store_true",
        help="只检查各项目的协议脚本副本是否落后模板，不写任何文件 / report script drift only",
    )
    parser.add_argument(
        "--sync-targets",
        default=None,
        help="--check-sync 的项目列表，逗号分隔；默认当前目录加 ~/projects/* / projects to check",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.template:
        templates = Path(args.template).expanduser().resolve() / "templates"
    else:
        templates = find_templates() or TEMPLATES

    if args.check_sync:
        if args.sync_targets:
            try:
                targets = [translate_target(item).resolve() for item in args.sync_targets.split(",")]
            except TargetPathError as error:
                print(error, file=sys.stderr)
                return 2
        else:
            targets = [Path.cwd().resolve()]
            projects = Path.home() / "projects"
            if projects.is_dir():
                targets.extend(
                    sorted(child.resolve() for child in projects.iterdir() if child.is_dir())
                )
        report = check_sync(list(dict.fromkeys(targets)), HERE)
        print_sync_report(report)
        return 0 if report["in_sync"] else 1

    try:
        target = translate_target(args.target or ".").resolve()
    except TargetPathError as error:
        print(error, file=sys.stderr)
        return 2
    name = args.name or target.name

    if not (templates / CONFIG_NAME).exists():
        print("找不到模板骨架 / template skeleton not found:", file=sys.stderr)
        print(f"  已尝试位置: {templates}", file=sys.stderr)
        print("  请在模板仓库里运行，或显式指定：", file=sys.stderr)
        print("    python3 ~/projects/agent-protocol-template/scripts/protocol_adopt.py \\", file=sys.stderr)
        print(f"      --target {target}", file=sys.stderr)
        print("  （也可以用环境变量 AGENT_PROTOCOL_TEMPLATE 指向模板仓库）", file=sys.stderr)
        return 2

    if target == templates.parent.resolve():
        print("这里是模板仓库本身，不能对自己采用协议。", file=sys.stderr)
        print("请用 --target 指向你的项目，例如：", file=sys.stderr)
        print("  python3 scripts/protocol_adopt.py --target ~/projects/my-project", file=sys.stderr)
        return 2

    if target.exists() and not target.is_dir():
        print(f"target is not a directory: {target}", file=sys.stderr)
        return 2

    adopted = (target / CONFIG_NAME).exists()
    plan = build_plan(target, templates, name, adopted)
    print_plan(target, name, plan, adopted)

    if args.dry_run:
        print("\n[dry-run] 什么都没写。去掉 --dry-run 就会真的执行。")
        return 0

    if not plan[CREATE_FILES] and not plan[UPDATE_FILES] and not plan[PRUNE_KEYS]:
        print("\n已经是最新状态，没有任何需要写入的内容。")
        next_steps(target)
        return 0

    if not args.yes and sys.stdin.isatty():
        try:
            answer = input("\n按回车执行；输入 n 放弃 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1
        if answer in {"n", "no", "q", "quit"}:
            print("已取消，什么都没写。")
            return 1

    target.mkdir(parents=True, exist_ok=True)
    apply_plan(target, templates, name, plan, adopted)
    print(f"\n完成：{'已刷新' if adopted else '已采用'} {target}")
    next_steps(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
