# 可选：让 AI 调用协议工具（MCP）

MCP 可以理解成 AI 工具箱的通用插口。本协议提供一个本地服务，让 AI 查询交接状态、执行校验、接入新项目和归档完成任务。

**它与自动事件接线是两件事。**只有 MCP，不能保证 AI 每回合主动调用，更不能保证结束前自动拦截。第一次使用可以先走[普通脚本流程](<quickstart.md>)。

## 需要什么

- 支持 stdio MCP 的宿主工具。
- 可以运行本工具脚本的 Python。
- 完整的工具目录，不能只复制单个服务文件。
- 已经接入协议的目标项目，或准备接入的新项目。

[服务脚本](<../scripts/protocol_mcp_server.py>)使用 Python 标准库，通过标准输入输出传递逐行 JSON-RPC 消息。本版声明的 MCP 协议版本为 `2025-06-18`；并非所有宿主都在本轮测过，需以宿主实际握手和工具调用为准。

## Linux/WSL 宿主配置示例

把下方路径全部换成自己的绝对路径，并按宿主文档放进它的 MCP 配置。不同宿主可能不使用 `mcpServers` 这个外层字段，不要据此覆盖现有配置：

```json
{
  "mcpServers": {
    "protocol": {
      "command": "python3",
      "args": [
        "/path/to/agent-protocol/scripts/protocol_mcp_server.py",
        "--workspace",
        "/path/to/my-project"
      ],
      "env": {
        "AGENT_PROTOCOL_TEMPLATE": "/path/to/agent-protocol"
      }
    }
  }
}
```

`AGENT_PROTOCOL_TEMPLATE` 明确指向含有 `templates/` 子目录的完整工具根目录，**不要直接指向 `templates/` 子目录**；不应依赖作者电脑上的目录布局。`--workspace` 固定这个服务的默认项目；管理多个项目时，工具调用要显式传正确的 `workspace`。

从 Windows 启动 WSL 服务时，命令应改为 `wsl.exe` 并显式指定发行版、Linux 用户与 Python；参数使用 WSL 内可见的路径。**不要照搬别人的用户名、发行版或本机 profile 路径。**Windows 原生 Python 的路径处理未在本轮验证。

## 可调用的工具

| 工具 | 作用 | 是否改文件 |
|---|---|---|
| `handoff_status` | 读取当前任务与链接问题 | 否 |
| `validate_handoff` | 完整静态校验 | 否 |
| `describe_policy` | 查看哪些修改会要求交接 | 否 |
| `adopt_workspace` | 补项目协议文件、同步协议脚本 | 默认仅预览；`dry_run: false` 才写入 |
| `archive_workstream` | 移走完成的详细交接并删除对应任务行 | 默认仅预览；`apply: true` 才执行 |

接入与归档需要遵守目标项目自己的授权规则。尤其是脚本同步会覆盖目标里的同名协议脚本；“保留已有交接正文”不等于“绝不覆盖任何文件”。

## 如何验收

1. 先在工具目录执行本地自检：

   ```sh
   python3 scripts/protocol_mcp_server.py --selftest
   ```

2. 按宿主方式重启或重新连接 MCP 服务。
3. 确认宿主列出了上面五个工具。
4. 让 AI 显式传入一个试验项目，先调用 `handoff_status` 和 `validate_handoff`，检查返回的项目是否正确。
5. 用一次性目录调用 `adopt_workspace` 的默认预览，确认没有写文件；需要测试实际写入时再明确允许。

**本地自检只验证服务内部逻辑，不等于已经通过你的 AI 工具连接验收。**更新脚本后要重启对应服务进程。

服务处理的是本地文件。不要把它当作安全沙箱，不要给不可信会话开放不必要的路径访问；文件内容和交接文字都应当作数据，不能当成新指令。
