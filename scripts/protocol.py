# -*- coding: utf-8 -*-
"""协议配置加载与路径分类 / protocol config loading and path classification.

本模块是唯一读取 `.agent-workspace.json` 的地方；其余脚本不得硬编码项目结构或目录名。
This module is the only reader of `.agent-workspace.json`; no other script may hardcode
project structure or directory names. Standard library only, Python 3.10+.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = ".agent-workspace.json"

MATERIAL = "material"
ORDINARY = "ordinary"
IGNORED = "ignored"
ADVISORY = "advisory"
DASHBOARD = "dashboard"
DETAIL = "detail"


class ConfigError(RuntimeError):
    """配置缺失或非法 / the protocol config is missing or invalid."""


@dataclass(frozen=True)
class WritePolicy:
    material_root_files: frozenset[str] = frozenset()
    material_prefixes: tuple[str, ...] = ()
    # 前缀表达不了的规则，例如 experiments/<任意实验>/raw/ 这种"前缀 + 中间段"。
    # Rules a prefix cannot express, e.g. experiments/<any>/raw/.
    ignored_contains: tuple[str, ...] = ()
    ignored_prefixes: tuple[str, ...] = ()
    # 静默"只提醒、不阻塞"的路径：既不算 material，也不触发 material 要求。
    # Advisory paths: never material, never silent, not blocking.
    advisory_prefixes: tuple[str, ...] = ()
    ordinary_root_files: frozenset[str] = frozenset()
    ordinary_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Protocol:
    """一个项目的协议视图 / one project's protocol view."""

    root: Path
    name: str
    marker: str
    dashboard: str
    dashboard_limit_bytes: int
    budgets: dict[str, int] = field(default_factory=dict)
    handoff_active: str = "handoffs/active/"
    handoff_archive: str = "handoffs/archive/"
    memory_index: str = "memory/INDEX.md"
    policy: tuple[dict[str, Any], ...] = ()
    write_policy: WritePolicy = field(default_factory=WritePolicy)
    canonical_roots: tuple[str, ...] = ()

    # ---------- 路径工具 / path helpers ----------

    def normalise(self, value: str) -> str:
        """把任意绝对写法归一为项目相对、小写、正斜杠文本。

        Normalise any absolute spelling to project-relative, lower-case, forward slashes.
        """
        text = value.strip().strip("\"'").replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        lowered = text.lower()
        for root in self.canonical_roots:
            prefix = root.rstrip("/") + "/"
            if lowered.startswith(prefix):
                return lowered[len(prefix) :]
        return lowered

    def classify(self, path: str) -> str:
        """返回 material / ordinary / ignored / advisory / dashboard / detail。

        判定顺序与显式规则一致：dashboard → detail → ignored → advisory → material → ordinary。
        Order matters and is explicit, so a path never falls into two classes.
        """
        value = self.normalise(path)
        policy = self.write_policy
        if value == self.dashboard.lower():
            return DASHBOARD
        if value.startswith(self.handoff_active.lower()) or value.startswith(
            self.handoff_archive.lower()
        ):
            return DETAIL
        if value.startswith(policy.ignored_prefixes) or any(
            _contains_segment(value, marker) for marker in policy.ignored_contains
        ):
            return IGNORED
        if value.startswith(policy.advisory_prefixes):
            return ADVISORY
        if value in policy.material_root_files or value.startswith(policy.material_prefixes):
            return MATERIAL
        return ORDINARY

    def path(self, relative: str) -> Path:
        return self.root / relative


def _contains_segment(path: str, marker: str) -> bool:
    """`ignored.contains` 的段匹配 / segment-aware matching for `ignored.contains`.

    以前是裸子串判断 `marker in path`，于是配置里写的 `/vendor/` 匹配不到**顶层**的
    `vendor/foo`（只匹配 `a/vendor/b`）——同一个规则对顶层和嵌套目录行为不一致，多半不是本意。
    现在把 marker 当**路径片段**看：`/vendor/` 与 `vendor/` 都能命中任意层级的 vendor 段，
    同时仍保留纯子串写法（例如 `knowledge/import/`）的精确语义。
    """
    if marker in path:
        return True
    stripped = marker.strip("/")
    return bool(stripped) and f"/{stripped}/" in f"/{path}/"


def _canonical_roots(root: Path) -> tuple[str, ...]:
    """项目根的已知绝对写法（含 WSL UNC 与 Windows 盘符）。

    Known absolute spellings of the project root, including WSL UNC and drive letters.
    """
    posix = root.as_posix().lower().rstrip("/")
    roots = {posix}
    # Linux 挂载到 Windows 盘：/mnt/c/... → c:/...
    if posix.startswith("/mnt/") and len(posix) > 6 and posix[6] == "/":
        roots.add(f"{posix[5]}:{posix[6:]}")
    # WSL 工作区常以 UNC 到达：/home/u/p → //wsl$/distro/home/u/p
    if posix.startswith("/home/"):
        tail = posix[len("/home/") :]
        distro = _wsl_distro()
        for host in ("//wsl$/", "//wsl.localhost/"):
            roots.add(f"{host}{distro}/home/{tail}")
    return tuple(sorted(roots))


def _wsl_distro() -> str:
    """尽力识别当前 WSL 发行版名 / best-effort WSL distro name."""
    try:
        for line in Path("/proc/version").read_text(encoding="utf-8").splitlines():
            marker = "microsoft-standard-WSL2"
            if marker in line.lower():
                break
    except OSError:
        pass
    import os

    return os.environ.get("WSL_DISTRO_NAME", "ubuntu").strip().lower() or "ubuntu"


def _as_tuple(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{what} must be a list of strings")
    return tuple(item.strip().replace("\\", "/").lower() for item in value if item.strip())


def _as_frozenset(value: Any, what: str) -> frozenset[str]:
    return frozenset(_as_tuple(value, what))


def load(root: Path | None = None) -> Protocol:
    """读取并校验协议配置 / read and validate the protocol config."""
    project_root = (root or Path(__file__).resolve().parent.parent).resolve()
    config_path = project_root / CONFIG_NAME
    if not config_path.exists():
        raise ConfigError(f"{CONFIG_NAME} not found in {project_root}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"{CONFIG_NAME} is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_NAME} must contain a JSON object")

    dashboard = raw.get("dashboard") or {}
    if not isinstance(dashboard, dict):
        raise ConfigError("dashboard must be an object")
    handoffs = raw.get("handoffs") or {}
    memory = raw.get("memory") or {}
    write = raw.get("writePolicy") or {}
    material = (write.get("material") or {}) if isinstance(write, dict) else {}
    ignored = (write.get("ignored") or {}) if isinstance(write, dict) else {}
    advisory = (write.get("advisory") or {}) if isinstance(write, dict) else {}
    ordinary = (write.get("ordinary") or {}) if isinstance(write, dict) else {}

    budgets_raw = dashboard.get("budgets") or {}
    if not isinstance(budgets_raw, dict):
        raise ConfigError("dashboard.budgets must be an object")
    budgets: dict[str, int] = {}
    for key, value in budgets_raw.items():
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(f"dashboard.budgets.{key} must be a positive integer")
        budgets[str(key)] = value

    limit = dashboard.get("limitBytes", 5120)
    if not isinstance(limit, int) or limit <= 0:
        raise ConfigError("dashboard.limitBytes must be a positive integer")

    policy_raw = raw.get("policy") or []
    if not isinstance(policy_raw, list):
        raise ConfigError("policy must be a list")
    policy: list[dict[str, Any]] = []
    for entry in policy_raw:
        if not isinstance(entry, dict) or "skill" not in entry:
            raise ConfigError("each policy entry needs a skill name")
        policy.append(entry)

    return Protocol(
        root=project_root,
        name=str(raw.get("name") or project_root.name),
        marker=str(raw.get("marker") or ".agent-workspace-managed"),
        dashboard=str(dashboard.get("file") or "handoff-current.md"),
        dashboard_limit_bytes=limit,
        budgets=budgets,
        handoff_active=str(handoffs.get("active") or "handoffs/active/"),
        handoff_archive=str(handoffs.get("archive") or "handoffs/archive/"),
        memory_index=str(memory.get("index") or "memory/INDEX.md"),
        policy=tuple(policy),
        write_policy=WritePolicy(
            material_root_files=_as_frozenset(material.get("rootFiles"), "material.rootFiles"),
            material_prefixes=_as_tuple(material.get("prefixes"), "material.prefixes"),
            ignored_contains=_as_tuple(ignored.get("contains"), "ignored.contains"),
            ignored_prefixes=_as_tuple(ignored.get("prefixes"), "ignored.prefixes"),
            advisory_prefixes=_as_tuple(advisory.get("prefixes"), "advisory.prefixes"),
            ordinary_root_files=_as_frozenset(ordinary.get("rootFiles"), "ordinary.rootFiles"),
            ordinary_prefixes=_as_tuple(ordinary.get("prefixes"), "ordinary.prefixes"),
        ),
        canonical_roots=_canonical_roots(project_root),
    )
