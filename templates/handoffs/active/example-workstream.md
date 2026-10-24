# Example workstream (delete me)

Status: In progress

初始化时自带的示例，用来验证协议链路是否连通。填上第一条真实 workstream 后即可删除。

## Latest verified state

- 这是模板示例，不是真实任务验收记录；接入后需运行 `python scripts/protocol_validate.py`，再填写实际结果。

## Decisions

- 记忆数据放在仓库 Markdown 文件里，工具只做约束与注入。

## Artifacts

- `.agent-workspace.json` — 唯一的项目参数出口
- `scripts/protocol_guard.py` — 分级交接守卫

## Open issues

- 尚未填写 `AGENTS.md` 的项目规则。
- `writePolicy` 的目录尚未按本仓库实际情况调整。

## Next action

按本仓库真实目录调整 `.agent-workspace.json` 的 `writePolicy`。

## Milestones

- <YYYY-MM-DD> — 从模板初始化
