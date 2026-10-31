# 快速上手：先试通交接，再接自动化

这份说明先走不依赖任何 AI 工具插件的路线。命令以 Linux/WSL 终端为例。Windows 宿主可以通过 WSL 使用；本轮没有验证原生 Windows Python 的完整流程，不要直接照抄 Linux 路径。

## 1. 先在一次性目录试跑

进入本工具根目录后执行。`mktemp` 会产生一个新的临时目录，不指向你正在工作的项目：

```sh
PROTOCOL_HOME="$PWD"
DEMO="$(mktemp -d)"
python3 "$PROTOCOL_HOME/scripts/protocol_adopt.py" --target "$DEMO/my-project" --dry-run
python3 "$PROTOCOL_HOME/scripts/protocol_adopt.py" --target "$DEMO/my-project" --yes
(cd "$DEMO/my-project" && python3 scripts/protocol_validate.py)
```

预期最后出现 `VALIDATION PASSED`。这是骨架检查，不代表示例内容已经符合你的真实项目，也不表示任何自动事件已经启用。

## 2. 给真实项目接入

建议先备份，或确认项目有可恢复的版本记录。在工具根目录运行，替换目标路径；有空格的路径要保留双引号：

```sh
python3 scripts/protocol_adopt.py --target "/path/to/my-project" --dry-run
python3 scripts/protocol_adopt.py --target "/path/to/my-project" --yes
(cd "/path/to/my-project" && python3 scripts/protocol_validate.py)
```

计划中需重点看：

- `created files`：要新建的文件或目录。
- `updated files`：会覆盖的同名协议脚本；不是项目交接正文。
- `pruned legacy keys`：将删除的废弃配置键，目前为顶层 `mcp`。
- 已存在的项目规则、当前任务表、详细交接和长期记录会保留；这些文档不会随脚本自动升级。

已有项目若有自己的当前任务表，不会再塞入模板示例任务。已经接入的项目再次运行，主要同步接入脚本列出的七个运行时脚本并补标记，不包括全局事件分发器和自测脚本，也不会把缺失的所有文档重新生成。因此，“重跑接入脚本”不是恢复误删记录的方法。

## 3. 填真实规则与重要目录

让 AI 阅读并按项目实际情况填写[工作约定模板](<../templates/AGENTS.md>)和[启动顺序模板](<../templates/AGENT-BOOTSTRAP.md>)对应的项目文件。

[配置示例](<../templates/.agent-workspace.json>)中，最需要理解的是：

```json
{
  "writePolicy": {
    "material": {
      "rootFiles": ["agents.md", "agent-bootstrap.md"],
      "prefixes": ["chapters/", "figures/", "scripts/"]
    },
    "ignored": {
      "prefixes": ["tmp/", "build/", "node_modules/"],
      "contains": ["/raw/", "/vendor/"]
    },
    "advisory": {"prefixes": ["scratch/"]},
    "ordinary": {"rootFiles": ["readme.md", ".gitignore"], "prefixes": []}
  }
}
```

这只是写作项目的局部配置示例，**不要用它覆盖整份配置**。需要保留项目名称、当前任务表、大小预算等其余字段。

- `material`：重要成果或治理文件。接好自动检查时，识别到这些文件的有效写入后，需要再更新两份交接。
- `ignored`：无需提醒的临时文件、依赖和原始数据目录。
- `advisory`：只提醒、不阻塞的临时工作。
- 其他路径一般按普通修改处理，不自动升级成“重要成果”。因此重要目录若漏配，守卫可能不会要求交接。

默认当前任务表上限为 5 KiB；大小按 UTF-8 字节计算，不是字数。不要为每个小步骤新开一条任务，也不要把全部历史塞进当前任务表。

## 4. 建立第一条真实交接

1. 参考[详细交接模板](<../templates/handoffs/TEMPLATE.md>)，在项目的 `handoffs/active/` 下新建一份任务记录。
2. 在项目当前任务表里加一行，保留示例表头。详细交接路径用项目相对路径，并像模板一样放在反引号里，便于现有检查器识别。
3. 确认新任务可读、链接正确后，再移除模板自带的示例任务及示例行；不要只删一边。
4. 运行目标项目的校验脚本。

详细交接保留五类信息：`Latest verified state`（已验证状态）、`Decisions`（决定）、`Artifacts`（产物）、`Open issues`（遗留问题）、`Next action`（下一步）；里程碑仅保留最近三条。详细排查过程和原始证据另放，不把交接写成聊天实录。

第一次可以这样告诉 AI：

> 先读启动顺序、项目约定和当前任务表，只打开与这次请求匹配的一份详细交接。完成实际修改后，更新对应详细交接和当前任务表。不要把未经验证的推测写成事实，不要自动把这次任务沉淀到长期记忆。

即使没有自动接线，这个使用约定也能独立工作；但靠的是人和 AI 主动遵守，不是机器强制。

## 5. 完成任务后归档

把完成任务的详细交接移到项目的 `handoffs/archive/`，同时删除当前任务表里的对应行，不留下“已完成”占位。下一会话就不会再把它当作进行中任务。

已经配置 [MCP 工具](<mcp-integration.md>)时，可以先让 AI 用 `archive_workstream` 预览，再明确 `apply: true` 执行。没有 MCP 时也可以按同样规则手工整理；移动与删除前遵守项目自己的授权约定。

## 多会话协作：不要一起抢写当前任务表

本版保留了[交接签收脚本](<../scripts/protocol_handoff_confirm.py>)。一个项目有多个会话同时修改成果时，推荐由一个交接负责人维护共享交接：

1. 成员汇报本次改动、待交接会话标识和 revision token（本批改动的签收编号）。成员不自行改守卫状态，也不自行签收。
2. 负责人核对成果，**在成员最新改动之后**更新相应详细交接和当前任务表。
3. 负责人按这批会话和 token 明确确认。新改动会生成新 token，旧签收不能代替新交接。

查询和签收命令在**已接入项目根目录**运行：

```sh
python3 scripts/protocol_handoff_confirm.py --pending
python3 scripts/protocol_handoff_confirm.py \
  --session MEMBER_SESSION --token REVISION_TOKEN \
  --leader-session LEADER_SESSION --detail handoffs/active/my-task.md
```

签收要求同一项目、对应的最新批次和先更新的两份交接，记录交接内容摘要。它只是交接凭证，不是代码评审，不证明成果正确。没有真实事件接线产生的会话状态时，这两条命令不是独立的安装测试。

## 更新协议脚本

v0.1.0（2026-10-31）的变化见[更新记录](<../CHANGELOG.md>)。模板目录和各项目里的脚本是**独立副本**；修改模板或推送 GitHub，都不会自动把其他项目升级。

接入脚本的同步清单为：[protocol.py](<../scripts/protocol.py>)、[protocol_guard.py](<../scripts/protocol_guard.py>)、[protocol_handoff_confirm.py](<../scripts/protocol_handoff_confirm.py>)、[protocol_context.py](<../scripts/protocol_context.py>)、[protocol_validate.py](<../scripts/protocol_validate.py>)、[protocol_mcp_server.py](<../scripts/protocol_mcp_server.py>)、[protocol_adopt.py](<../scripts/protocol_adopt.py>)。全局事件分发器和自测脚本留在完整工具目录，不随项目接入同步；需要另行维护。

只检查你指定的项目：

```sh
python3 scripts/protocol_adopt.py --check-sync --sync-targets /path/to/my-project
```

`--check-sync` 只报告字节是否相同，不更新文件。输出“有差异/缺失”时先核对项目定制；差异不一定表示版本落后。

如果省略 `--sync-targets`，当前实现会检查当前目录和 `~/projects/` 下的直接子目录，不会只检查眼前一个项目。为了范围清楚，建议显式指定；多个路径用逗号分隔。

需要更新时，先备份并比较项目定制内容，尤其是守卫、状态目录和交接签收逻辑；有定制时不能盲目运行接入脚本。从新版本工具根目录预览，确认后才同步七个同名运行时脚本：

```sh
python3 scripts/protocol_adopt.py --target "/path/to/my-project" --dry-run
python3 scripts/protocol_adopt.py --target "/path/to/my-project" --yes
(cd "/path/to/my-project" && python3 scripts/protocol_validate.py)
```

接入会覆盖清单内有差异的脚本，也可能删除明确废弃的顶层 `mcp` 键；会保留项目 `writePolicy`、已有治理文档、交接、长期记录和原始成果，不用模板正文覆盖它们。新版本守卫状态按项目根目录隔离，即使项目名称相同也不共用状态；升级后应新开会话验证，不能把旧临时状态当作本版已完成交接的证明。更新了 MCP 服务脚本后，还需要择机重启对应的 MCP 进程；磁盘上的新代码不会自动进入已运行的进程。

## 停用而不丢掉记录

1. **先停自动接线**：在宿主配置里停用本协议事件桥或移除本协议命令；不要连其他工具的配置一起删。全局分发器服务多个项目时，应规划项目级退出，不要误停其他项目。
2. 移除不再需要的 MCP 注册或技能注册；它们是独立接入，停一个不代表另一个也停了。
3. 保留你写下的交接、长期知识和项目约定。若想完全退出，先备份，再按清单移走协议脚本、配置、标记和不再使用的模板，调整项目约定中的协议要求。不要整批删除项目自己的文档。
4. 新开会话确认不再注入本协议提醒；原始项目功能仍应正常。

**仅删除采用标记并不能可靠停用。**全局分发器主要根据项目配置决定是否调用脚本，而不是只看标记是否存在。

## 常见疑问

- **校验通过了，为什么没有自动提醒？** 项目文件与事件接线是两件事。请看 [DSH 接入边界](<dsh-integration.md>)；只注册 MCP 也不会自动在结束前拦截。
- **提示没写交接后怎么收尾？** v0.1.0 在同一连续缺口内最多拦一次、随后提醒一次，再静默放行；改文件或继续调用工具不恢复额度，真正无缺口的结束检查才重置。首次只做一次有界补正：检查最新成果后是否更新了两份交接、工具是否成功、目标参数是否完整；仍有缺口就报告并结束，不继续调试守卫或反复重写。放行不表示待交接状态已清除。终端 Python 识别有边界；优先用原生文件编辑工具，不靠 `touch`、原样写回或重复无变化命令应付。
- **为什么不自动保存一切？** 这是精选交接，不是全量聊天备份。长期知识只在用户明确要求时维护。
- **换另一个 AI 工具能用吗？** 文本模板可以迁移，事件接线需要单独适配与实测。能读取文件不等于具有自动阻塞能力。
