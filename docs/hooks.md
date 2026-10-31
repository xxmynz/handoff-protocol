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
| `session_id` | 是 | 运行时状态按项目根目录与会话隔离 | 同项目缺失标识的会话共用 `unknown`，记账会串 |
| `tool_name` | PostToolUse 必需 | 判工具类型：`edit`/`write` 这类原生文件工具，`bash`/`exec_command` 这类 shell | 该工具被忽略 |
| `tool_input` | PostToolUse 必需 | 通知路径（`file_path` 等）或命令文本 | 有写入迹象时报 `unresolved local write`，保守要求交接 |
| `tool_response` | PostToolUse 建议 | 判断这次调用是否失败/只读 | 无法判断，按成功处理 |
| `cwd` | SessionStart 建议 | 分发器据此找项目根 | 退回进程当前目录和环境变量 |
| `stop_hook_active` | Stop 可选 | 宿主是否已在处理递归结束检查 | 为 `true` 时首次也不阻塞；缺失或为 `false` 时，守卫自行限制一次阻塞 |

## 输出

**SessionStart** —— 模型要看到的东西放在这里，宿主必须把它注入上下文：

```json
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "……当前任务表……"}}
```

**PostToolUse** —— 不输出。宿主别因为 stdout 为空就报错。

**Stop** —— 可能阻塞、提醒或无输出。下面是不同调用的输出示例，不是一次输出两行：

```json
{"decision": "block", "reason": "Handoff guard: …… Make at most one bounded handoff correction, then finish this turn. If still unresolved, report the gap in your final response. Do not keep rewriting handoffs or debug/modify the guard just to pass this check."}
{"systemMessage": "Handoff guard: ……; automatic retries stopped; handoff is still unresolved; report the gap and finish this turn"}
```

`block` 的意思是“把 `reason` 给模型，让它再做一次有界交接补正”。宿主不支持阻塞语义时，不能保证触发补正；只打印到日志不等于模型收到要求。普通或低影响修改也可能输出非阻塞的 `Handoff reminder only` 提醒。

## 一次补救与状态保留

v0.1.0 的 `STOP_BLOCK_LIMIT = 1`，额度按同一会话的**连续未解决区间**计算：

1. 首次缺口最多阻塞一次，`reason` 要求只做一次有界补正；仍有缺口，就在最终答复报告并结束，不为过关调试守卫或反复重写交接。
2. 后续最多输出一次警告，同时写 stderr；再后续 Stop 静默放行。`stop_hook_active: true` 表示递归检查，首次也不阻塞，直接走一次警告。
3. 文件时间或大小变化、普通 `PostToolUse`、新的 material token 均不恢复额度。只有真正没有 issues 的 Stop 才重置，让同一会话后续新工作仍有一次机会。
4. 放行不会清除 `material_pending`，不表示交接通过。新改动仍需正常交接或由负责人按最新 token 签收。

终端 shell 交接须工具成功、目标明确且文本确实变化；原生文件工具沿用成功路径事件规则。常见直线内联 Python 可识别，`touch`、原样写回、打印路径、其他会话的更新和失败命令不能代替本会话交接。命令识别按 shell 命令位置解析；搜索或引号正文提到 `cp`、`tee`、`touch` 不当作写入。这是有限识别范围，不保证任意脚本无漏判。

## 最小接法

把下面两类调用接进你自己的钩子配置，并传递对应事件。`<repo>` 指这个仓库放的位置，`<project>` 指你的项目。

```sh
# 会话开始
python3 <repo>/scripts/protocol_dispatch.py    # 读 stdin，注入 additionalContext
# 用完工具之后 / 会话要结束之前：同一个入口，靠 hook_event_name 分流
python3 <repo>/scripts/protocol_dispatch.py
```

不想用分发器、只在单个项目里接，就在已接入项目根目录调项目自己那份副本。替换下面的占位路径；第二条只模拟一次已成功的重要文件写入事件，不实际写文件：

```sh
echo '{"session_id":"t1","hook_event_name":"SessionStart","cwd":"<project>"}' \
  | python3 <project>/scripts/protocol_context.py

echo '{"session_id":"t1","hook_event_name":"PostToolUse","tool_name":"write","tool_input":{"file_path":"<project>/chapters/01.md"}}' \
  | python3 <project>/scripts/protocol_guard.py
```

第二条命令预期静默退出；静默本身不证明记账成功。接着用相同 `session_id` 发送 `Stop` 事件；若目标路径被配置为重要文件、缺交接且该会话额度未用完，应看到 `decision: block`。

## 已知坑

- **正确传递递归标记**。有些宿主把 `stop_hook_active` 固定为 `false`；守卫仍自行限制为一次阻塞，不靠文件变化重置。宿主真实处于递归 Stop 时应传 `true`，避免再触发阻塞。
- **`tool_response` 被压成纯文本时**，守卫看不出来内容是不是正文：`Traceback` 字样只在 shell 工具里算失败，文件工具的正文里出现它不算。
- **`systemMessage` 不一定被注入**。已知本地 DSH Stop 桥不注入它，所以关键补正与收尾要求必须保留在首次 `reason`；缺口警告另写 stderr 供宿主日志诊断，不能当作模型已收到提醒。
- **子进程别拖**。分发器给每个脚本 8 秒，超时就当成没做事（`/tmp/protocol-dispatch.log` 里有记录）。

## 自测

```sh
python3 scripts/protocol_dispatch_selftest.py    # 分发器：找项目、跳过、转发
python3 scripts/protocol_guard_selftest.py       # 守卫：分级、拦截、确认放行
```

两个都打印 `all checks passed` 再谈接线。接线后，在新会话里首次改完重要文件且未补交接，却完全没有阻塞或诊断提醒时，应检查 Stop 接线和宿主日志里的 `skip:`、`failed:`。已用完一次补救额度或传入递归标记时允许结束是预期行为，不能仅凭“能结束”判断接线失效。
