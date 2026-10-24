# AGENT-BOOTSTRAP.md — Cross-agent entry point

{{PROJECT_NAME}} 的静态、Agent 无关入口。它只说明接管顺序，不记录任务进度。

## Read order

1. `AGENT-BOOTSTRAP.md`：确认接管协议。
2. `AGENTS.md`：读取项目长期规则与授权边界。
3. `handoff-current.md`：查看 active/waiting workstream。
4. 按用户自然语言匹配一个 `handoffs/active/*.md`；不要要求任务编号，也不要读取全部 active handoff。
5. 按任务读取一个或少数 `skills/<skill>/SKILL.md`。
6. 只有用户要求历史回忆，或 active handoff 明确引用历史来源时，才查询 `memory/INDEX.md` 或 `handoffs/archive/`。

## Source-of-truth order

- 永久规则：`AGENTS.md`
- 当前状态：`handoff-current.md` + 一个匹配的 `handoffs/active/*.md`
- Skills 权威源：`skills/`
- 稳定事实与可复用摘要：`memory/INDEX.md`
- 历史讨论：`handoffs/archive/`

## Working contract

- 项目内部使用相对路径；向用户交付时核实并给当前电脑上的完整绝对路径。
- 范围内只读检查、本地编辑和非破坏性验证可直接执行；外部发送、删除、覆盖原稿或扩大范围先确认。
- 正式有效执行后更新详细交接与 dashboard；完成项立即归档并从 dashboard 删除。

## First turn

确认当前项目根目录后，按上述顺序恢复当前任务。如果两个 workstream 确实同时匹配，立即询问一个高影响澄清问题；否则直接继续，不重做已经验证完成的工作。
