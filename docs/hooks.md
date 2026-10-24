# 钩子契约：让守卫在自己用的 AI 工具里生效

`protocol_guard.py` 自己不主动跑。它只在宿主（AI 工具）的**事件钩子**里被调用：宿主在"用完某个工具之后"和"会话要结束之前"把一份 JSON 从 stdin 喂进来，脚本看情况输出一句话或拦住结束。

这份文件写的就是这个接口。仓库里没有带任何宿主的桥——你用的工具怎么配钩子，按它的官方文档来，接上这三个调用点即可。

## 三个调用点

| 时机 | 跑哪个脚本 | 做什么 |
|---|---|---|
| 会话开始 | `scripts/protocol_context.py` | 把当前任务表注入模型上下文，并在临时目录记一份基线 |
| 用完工具之后 | `scripts/protocol_guard.py` | 只记账：哪些文件被改了、是不是重要文件。不输出 |
| 会话要结束之前 | `scripts/protocol_guard.py` | 判定：重要文件改了却没更新交接，就拦一次 |

脚本永远退出码 0 —— 钩子不能把宿主卡死。带 `.agent-workspace.json` 的项目之外，它们什么都不做。

多项目共用一个全局钩子时，中间加 `scripts/protocol_dispatch.py`：它按会话工作目录往上找 `8` 层，找到带 `.agent-workspace.json` 的项目就把 stdin 原样交给**那个项目自己的**脚本副本；找不到就静默跳过。中间层，不是必须。

## 输入

stdin 一个 JSON 对象（你的工具给的名字可能不同，改名字的时候改这一层就行）：

| 字段 | 必需 | 用途 | 缺了会怎样 |
|---|---|---|---|
| `hook_event_name` | 是 | `SessionStart` / `PostToolUse` / `Stop` | 什么都不做 |
| `session_id` | 是 | 运行时状态按会话隔离 | 所有会话共用 `unknown`，记账会串 |
| `tool_name` | PostToolUse 必需 | 判工具类型：`edit`/`write` 这类原生文件工具，`bash`/`exec_command` 这类 shell | 该工具被忽略 |
| `tool_input` | PostToolUse 必需 | 通知路径（`file_path` 等）或命令文本 | 有写入迹象时报 `unresolved local write`，保守要求交接 |
| `tool_response` | PostToolUse 建议 | 判断这次调用是否失败/只读 | 无法判断，按成功处理 |
| `cwd` | SessionStart 建议 | 分发器据此找项目根 | 退回进程当前目录和环境变量 |
| `stop_hook_active` | Stop 可选 | 上次拦截后宿主是否已重跑 | 守卫自己数拦截次数，最多 3 次后降级为提醒 |

## 输出

**SessionStart** —— 模型要看到的东西放在这里，宿主必须把它注入上下文：

```json
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "……当前任务表……"}}
```

**PostToolUse** —— 不输出。宿主别因为 stdout 为空就报错。

**Stop** —— 两种：

```json
{"decision": "block", "reason": "Handoff guard: ……"}
{"systemMessage": "Handoff reminder only: ……"}
```

`block` 的意思是"这一轮不许结束，把 `reason` 给模型，让它再跑一轮补交接"。宿主不支持这个语义（只会把输出当日志），守卫就退化成"结束时提醒一次"。

## 最小接法

把下面三行照抄进你自己的钩子配置即可。`<repo>` 指这个仓库放的位置，`<project>` 指你的项目。

```sh
# 会话开始
python3 <repo>/scripts/protocol_dispatch.py    # 读 stdin，注入 additionalContext
# 用完工具之后 / 会话要结束之前：同一个入口，靠 hook_event_name 分流
python3 <repo>/scripts/protocol_dispatch.py
```

不想用分发器、只在单个项目里接，就直接调项目自己那份副本：

```sh
echo '{"session_id":"t1","hook_event_name":"SessionStart","cwd":"<project>"}' \
  | python3 <project>/scripts/protocol_context.py

echo '{"session_id":"t1","hook_event_name":"PostToolUse","tool_name":"write","tool_input":{"file_path":"<project>/chapters/01.md"}}' \
  | python3 <project>/scripts/protocol_guard.py
```

第二条命令静默退出 = 记账成功。第三条把 `hook_event_name` 换成 `Stop`，就该看到 `decision: block`。

## 已知坑

- **别在 Stop 里传死循环标记**。有些宿主把 `stop_hook_active` 写死成 `false`，守卫发现它不可信就自己数拦截次数（`STOP_BLOCK_LIMIT = 3`），超过上限只提醒、不再拦。所以不传这个字段是安全的。
- **`tool_response` 被压成纯文本时**，守卫看不出来内容是不是正文：`Traceback` 字样只在 shell 工具里算失败，文件工具的正文里出现它不算。
- **`systemMessage` 不一定被注入**。宿主不认这个字段时，守卫同时往 stderr 写一行，那是它在宿主日志里唯一能留下的证据。
- **子进程别拖**。分发器给每个脚本 8 秒，超时就当成没做事（`/tmp/protocol-dispatch.log` 里有记录）。

## 自测

```sh
python3 scripts/protocol_dispatch_selftest.py    # 分发器：找项目、跳过、转发
python3 scripts/protocol_guard_selftest.py       # 守卫：分级、拦截、确认放行
```

两个都打印 `all checks passed` 再谈接线。接线之后最容易发现的症状是改完重要文件、会话却能直接结束——那说明 Stop 那条没接上，去宿主日志里看有没有 `skip:` 或 `failed:` 开头的行。
