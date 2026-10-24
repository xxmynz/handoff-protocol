# -*- coding: utf-8 -*-
"""责任人明确确认一批成员改动；临时凭据不是文件系统安全边界。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def snapshot(proto, paths):
    """内容摘要只覆盖声明路径；禁止越过项目根或读取过大文件。"""
    result = {}
    root = proto.root.resolve()
    for relative in sorted(set(paths)):
        item = (root / relative).resolve()
        if not item.is_relative_to(root):
            raise ValueError("confirmation path escapes workspace")
        if not item.exists():
            # normalise 会转小写；在大小写敏感的宿主上找回真实名称。
            item = root
            for part in Path(relative).parts:
                if part in (".", ".."):
                    raise ValueError("invalid confirmation path")
                exact = item / part
                if exact.exists():
                    item = exact
                    continue
                matches = [p for p in item.iterdir() if p.name.lower() == part.lower()] if item.is_dir() else []
                if len(matches) > 1:
                    raise ValueError("ambiguous path casing")
                item = matches[0] if matches else exact
            item = item.resolve()
            if not item.is_relative_to(root):
                raise ValueError("confirmation symlink escapes workspace")
        if not item.exists():
            result[relative] = None  # 删除也是一批明确改动。
            continue
        if not item.is_file() or item.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("confirmation only supports files up to 64 MiB")
        digest = hashlib.sha256()
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result[relative] = digest.hexdigest()
    return result


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def receipt_path(state_path):
    return state_path.parent / "confirmations" / state_path.name


def confirmed(proto, state_path, state):
    receipt = read_json(receipt_path(state_path))
    if not state.get("material_token") or receipt.get("token") != state["material_token"]:
        return False
    if receipt.get("workspace") != str(proto.root.resolve()) or not receipt.get("leader_session"):
        return False
    paths = receipt.get("paths")
    if not isinstance(paths, dict) or not set(state.get("material_paths", [])).issubset(paths):
        return False
    try:
        # 不把其他校验错误顺便放行；复用协议唯一校验实现。
        import protocol_validate
        return not protocol_validate.collect_problems(proto) and snapshot(proto, paths) == paths
    except (OSError, ValueError, ImportError):
        return False


def confirm(proto, target_path, token, leader_path, detail, collect_problems):
    state = read_json(target_path)
    leader = read_json(leader_path)
    if not state.get("material_pending") or not token or state.get("material_token") != token:
        raise ValueError("pending revision changed; inspect the current member report")
    workspace = str(proto.root.resolve())
    if state.get("workspace") != workspace or leader.get("workspace") != workspace:
        raise ValueError("member/leader state is not from this workspace")
    if not leader.get("dashboard_updated") or detail not in leader.get("detail_paths", []):
        raise ValueError("leader must update this detail and dashboard before confirming")
    revision_at = state.get("material_revision_at")
    if not isinstance(revision_at, int) or leader.get("dashboard_updated_at", 0) < revision_at or leader.get("detail_updated_at", {}).get(detail, 0) < revision_at:
        raise ValueError("leader must update both handoffs after this member revision")
    if not proto.path(detail).is_file() or proto.classify(detail) != "detail":
        raise ValueError("confirmation requires an existing detailed handoff")
    problems = collect_problems(proto)
    if problems:
        raise ValueError("protocol validation failed: " + "; ".join(problems))
    paths = snapshot(proto, [*state.get("material_paths", []), proto.dashboard, detail])
    current = read_json(target_path)
    if current.get("material_token") != token or not current.get("material_pending"):
        raise ValueError("member changed while confirmation was being prepared")
    receipt = {"workspace": workspace, "token": token, "leader_session": leader_path.stem,
               "detail": detail, "paths": paths}
    path = receipt_path(target_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".confirm-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return receipt


def main():
    import protocol
    import protocol_guard
    import protocol_validate
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--token")
    parser.add_argument("--leader-session")
    parser.add_argument("--detail")
    args = parser.parse_args()
    proto = protocol.load()
    root = protocol_guard.temp_root(proto.name, proto.root)
    if args.list:
        pending = []
        writers = []
        for path in sorted(root.glob("*.json")):
            state = read_json(path)
            if state.get("material_pending") and state.get("workspace") == str(proto.root.resolve()):
                pending.append({"session": path.stem, "token": state.get("material_token"), "paths": state.get("material_paths", [])})
            if state.get("workspace") == str(proto.root.resolve()) and state.get("dashboard_updated") and state.get("detail_paths"):
                writers.append({"session": path.stem, "details": state["detail_paths"]})
        print(json.dumps({"pending": pending, "writers": writers}, ensure_ascii=False))
        return 0
    if not all((args.session, args.token, args.leader_session, args.detail)):
        parser.error("confirmation requires session, token, leader-session and detail")
    target = protocol_guard.state_path(root, args.session)
    leader = protocol_guard.state_path(root, args.leader_session)
    try:
        receipt = confirm(proto, target, args.token, leader, args.detail, protocol_validate.collect_problems)
    except (ValueError, OSError) as error:
        print(str(error))
        return 2
    print(json.dumps({"confirmed": args.session, "token": receipt["token"], "detail": args.detail}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
