# Handoff Protocol

换一个 AI 会话，不用从头解释项目做到哪了。

一套放在项目文件夹里的交接约定、文本模板和 Python 脚本：AI 把"做到哪、验证过什么、下一步做什么"写进固定文件，下个会话读这几份文件接着干。

仓库和首页叫 Handoff Protocol；脚本、配置键和已接入的项目里，内部名仍是 `agent-protocol`，同一套东西，不用改名。

首个带版本号里程碑：**v0.1.0（2026-10-31）**。变化与升级注意事项见[更新记录](<CHANGELOG.md>)；已接入项目不会自动升级。

## 它管什么

| 文件 | 放什么 | 什么时候读 |
|---|---|---|
| `handoff-current.md` | 当前在做的任务、简短状态、下一步（有大小上限，默认 5 KB） | 每个新会话开头 |
| `handoffs/active/*.md` | 某个任务的详细交接：已验证状态、决定、遗留问题 | 只看和这次请求匹配的一份 |
| `handoffs/archive/` | 已完成任务 | 需要追溯时再翻 |

项目长期规则放 `AGENTS.md`；长期知识放 `memory/`，用户点名才写，不自动堆积。

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/protocol_adopt.py` | 给项目补齐骨架和配置 |
| `scripts/protocol_validate.py` | 检查结构、大小预算、链接 |
| `scripts/protocol_guard.py` | 改了重要文件却没更新交接时，给一次补救机会（需要宿主接线） |
| `scripts/protocol_dispatch.py` | 供宿主的事件钩子调用，按目录找到已接入的项目 |
| `scripts/protocol_mcp_server.py` | 提供 MCP 工具：查状态、校验、归档 |

只依赖 Python 标准库，源码按 3.10+ 写。已有 Linux/WSL + Python 3.14.4 实测记录；Python 3.10 只做过语法检查，Windows 原生、macOS 和其他宿主未实测。版本验收范围见[验证记录](<docs/verification.md>)。

## 第一次用

```sh
python3 scripts/protocol_adopt.py --target /path/to/my-project --dry-run   # 只看计划，不写文件
python3 scripts/protocol_adopt.py --target /path/to/my-project --yes       # 按计划写入
cd /path/to/my-project && python3 scripts/protocol_validate.py             # 在目标项目内校验
```

看到 `VALIDATION PASSED` 后再做三件事：

1. 填 `AGENTS.md` 和 `AGENT-BOOTSTRAP.md` 的真实规则，别留 `<语言>` 这类空位。
2. 把 `.agent-workspace.json` 里 `writePolicy.material.prefixes` 换成真正重要的目录——写作用 `chapters/`、`figures/`，不一定是 `src/`；构建产物、缓存放进 `ignored`。
3. 把示例任务换成第一个真实任务，同步维护当前任务表和详细交接，再跑一次校验。

不想碰终端，就把这段交给有本地文件权限的 AI：

> 读这套工具的 README 和 docs/quickstart.md，用接入脚本给我指定的项目补齐骨架。先给 dry-run 计划；不要覆盖已有的项目规则、交接和长期记录；按项目真实目录调整重要文件范围；最后运行校验。自动接线没验证就说没验证。

## 边界

能做的：新会话一眼看到进度；详细交接只在相关时才读，不会一次吞掉一屏；识别到重要文件修改但缺交接时，给 AI 一次补救机会。常见直线内联 Python 写交接也能识别——命令成功、目标明确且交接文本确实改变才算。

**一次补救怎么收尾：**同一会话的连续未解决区间，最多拦一次，要求只做一次有界补正；仍有缺口就在最终答复报告并结束，不为过关继续调试守卫或反复重写。随后最多提醒一次，再次结束静默放行；改文件或继续调用工具不会恢复额度。只有一次真正没有缺口的结束检查，才恢复后续新工作的补救机会。放行不清除待交接状态，也不表示交接已通过。

不能做的：不保存聊天记录和原始成果；不判断交接内容真假，也不保证没有漏判；只观察 `handoff-current.md` 和 `handoffs/active/` 直接下的 Markdown，单文件 64 KiB 封顶；`protocol_guard.py` 要靠宿主的事件钩子，仓库不带任何宿主的桥，得按[钩子契约](<docs/hooks.md>)自己接；校验器不扫敏感信息，也不看版本历史。

接入时会同步覆盖七个同名运行时脚本（不含分发器和自测），并可能清掉配置里明确废弃的顶层 `mcp` 键；不覆盖已有项目规则、交接和长期记录。升级先看 `--dry-run`，定制过守卫、状态目录或签收逻辑的项目先比差异，不能盲目接入。步骤见[更新协议脚本](<docs/quickstart.md#更新协议脚本>)。

## 更多

- 完整操作、停用方法、多会话协作：[快速上手](<docs/quickstart.md>)
- MCP 接入：[docs/mcp-integration.md](<docs/mcp-integration.md>)；DSH 自动接线：[docs/dsh-integration.md](<docs/dsh-integration.md>)
- 钩子怎么接、有什么坑：[钩子契约](<docs/hooks.md>)
- 本轮实测结果和未验证部分：[验证记录](<docs/verification.md>)
- 自己验一遍：`protocol_validate.py`、四个 `*_selftest.py`、`protocol_mcp_server.py --selftest`

有问题开 issue，看到会回，不承诺响应时间。

## 许可证

[MIT](<LICENSE>)：随便用，商用、改造、再发布都行，保留版权声明即可。
