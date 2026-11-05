# ACP Room — 把多个 AI 放进同一个房间

一个本机跑的聊天平台：你和几个**真实的编程 agent**（Claude Code / Codex / Gemini / pi / omp / Hermes）在一个房间里轮流说话。
不是模拟角色，是真的进程——它们有自己的工具、自己的记忆、自己的模型配置。

**English: [README.en.md](README.en.md)**

## 现在就能跑

```powershell
git clone https://github.com/ovo-pwp/acp-room.git acp-room
cd acp-room

python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # 或者 .venv\Scripts\activate 后 pip install

copy config.example.json config.json            # 然后按下面「加一个 agent」改成你自己的
.\start.ps1                                     # 打开 http://127.0.0.1:8770
.\start.ps1 -Port 9000                          # 换端口
.\start.ps1 -NoBrowser                          # 不自动开浏览器
```

`config.json` 不在版本库里——它描述的是「这台机器上有哪些 agent、命令怎么写」，
每台机器都不一样。`config.example.json` 是一份可以直接抄的骨架。

启动后左侧会逐个变绿（在线）。`npx` 首次拉包要十几秒，之后 1-2 秒。

## ⚠️ 先读这段：它会真的动你的文件

这不是一个「聊天壳子」。房间里每个 agent 都是**你机器上的真实进程**，有 shell、
有文件读写、有网络。你对它们说的话，最终会变成那个目录里的实际操作。

三条默认设置请按需收紧：

| 设置 | 默认 | 收紧成什么 |
|---|---|---|
| `config.json` → `cwd` | 模板里是 `"."`（**就是仓库自己**） | 改成你的项目目录。**别让它停在这里**——agent 会改这个项目自己的源码 |
| `agents[].permission` | `allow`（自动放行工具调用） | `ask`：每次弹窗问你；`deny`：全拒 |
| `policy` 块 | 危险命令（`rm -rf`、`format-volume`、`C:/Windows`、`~/.ssh/`）自动拒绝，写操作转人工 | 把更多规则放进 `auto_deny` |

`permission: "ask"` 时，权限请求会走界面上的内联卡片，你点「允许」才会执行。

**不要把这个服务直接暴露到公网。** 它没有用户系统、没有鉴权、默认只监听
`127.0.0.1`——这是有意的。要远程用，请放在 SSH 隧道或带认证的反向代理后面。

## 模型和密钥从哪来？—— 这个项目不管

**ACP Room 不提供模型，也不存密钥。** 它做的事只有一件：spawn 你配置的那条命令，
然后用 ACP 协议跟它说话。

模型、密钥、计费、上下文窗口——全部由**那个 agent 自己的 CLI** 决定：

| agent | 它的模型配置在哪 |
|---|---|
| Claude Code | `~/.claude/settings.json`，或 `claude login` |
| Codex | `~/.codex/config.toml` |
| Gemini CLI | `~/.gemini/` |
| pi | `~/.pi/agent/models.json` + `settings.json` |
| omp | `~/.omp/agent/models.yml` + `config.yml` |

所以「加一个 agent」之前，**先在终端里手动把它跑起来、确认它能答话**。
那一步通了，房间这边只需要写对 `cmd`。

### 三种常见的接法

1. **用 CLI 自己的登录**（最省事）——`claude login`、`codex login`，模型直连官方。
2. **指向一个 OpenAI 兼容端点**——官方 API、Ollama、LM Studio、LiteLLM / one-api 之类的网关都行。
   改的是**那个 CLI 的配置文件**，不是 `config.json`。
3. **从房间这边传环境变量**（适合"这个房间用 A 账号，我平时用 B 账号"）：

```json
{
  "id": "kimi",
  "cmd": ["kimi", "acp"],
  "env": { "KIMI_API_KEY": "sk-xxxx", "KIMI_BASE_URL": "https://..." }
}
```

> 注意：`config.json` 已进 `.gitignore`，不会提交。但如果你更希望密钥不落在项目目录里，
> 就还是配在 CLI 自己的配置文件。

### ⚠️ 「在线」不等于「能用」

这是最容易踩空的一步。agent 显示绿色只说明 **ACP 握手成功、进程活着**——
不证明它的模型 key 是通的。

实测过的样子：

```
状态：在线
发言：402 Insufficient Balance (request_id: ...)
      401 Authentication Fails, Your api key: ****6ad2 is invalid
```

握手成功 + 每轮模型报错。所以加完 agent 之后，别只看绿灯：

```
@新agent 一句话：你的名字和模型
```

答不上来就去「⚙ 设置与面板 → 配置」看它的模型列表是不是 0 个。

（这类错误正文会被**回复守卫**拦下，不进转录——所以你看到的是"它没说话"，
而不是一段 401 混在对话里。）

## 为什么是 ACP 而不是自己造协议

**ACP（Agent Client Protocol）** 已经是事实标准——Zed、Neovim、Obsidian、VS Code、Emacs、
Windows Terminal 都用它接 agent。它规定了 client 和 agent 之间怎么握手、开会话、发消息、流式回传、
申请权限。

好处很直接：**你不用为每个 agent 写适配器。** 我实测这台机器上四个 agent 全都能直接以 ACP server 起来：

```
pi       npx -y --package=pi-acp -- pi-acp
omp      omp acp                          （omp 内置 --acp，不需要适配器）
claude   npx -y --package=@agentclientprotocol/claude-agent-acp -- claude-agent-acp
codex    npx -y --package=@agentclientprotocol/codex-acp -- codex-acp
```

要加新 agent，先查它有没有 ACP 支持：<https://agentclientprotocol.com/overview/agents>
或 `acpx` 的内置注册表（30 多个：gemini / cursor / copilot / qwen / kimi / kiro / goose / opencode / droid / iflow / trae …）。

## 加一个 agent

### 最省事：在界面里加（不用重启）

左侧栏底部 →「**添加 agent**」→ 展开后：

1. 从下拉里选一个预设（内置 9 个：kimi / qwen / gemini / cursor / copilot / goose / opencode / droid / dsh）
   —— 会自动填好 id、显示名、启动命令
2. 点「**添加**」

它先**探活**（真的把命令跑起来握一次手），通过了才写进 `config.json` 并**当场接进房间**：
状态先显示「连接中…」，几秒后变「在线」。**不用重启，也不用再点「重连全部 agent」**。

探活失败的写不进去——这是刻意的：一条写错的命令如果先进了配置，
下次启动整个房间都起不来，而你只会在日志里看到一行 Traceback。

> 只启动**这一个**新 agent，不会打断正在对话的其他人。刻意没有复用
> 「重连全部」——那是为了加一个人掐掉另外五个的 session，代价不对。

新 agent 的上下文从**当前转录末尾**开始，不回放历史——它不该替自己没在场的讨论发言。

### 想给它一个人格：表单里就能填

「添加 agent」面板里有个折叠块 **人格（可选）**，四个字段：

| 字段 | 作用 | 不填会怎样 |
|---|---|---|
| 颜色 | 头像 + 左侧色条 + 名字颜色 | 自动从配色板里挑一个没被占用的 |
| `tag` | 一句话分工，**会推给房间里其他人** | 名册里显示为空 |
| `role` | 第二人称的角色设定，**只推给它自己** | 落到 `core/persona.py` 的通用兜底 |
| `style` | 说话风格 | 无 |

颜色是自动挑的：打开面板时会读现有 agent 的颜色，从十色调色板里选一个没被占用的。
撞色不是小事——房间靠色条区分谁在说话，两个同色等于抹掉其中一个的身份。

不填也能用，只是它会变成「通用人格 + 灰色头像」，和别人的腔调混在一起。
`role`/`tag` 的作用见下面「设计上想清楚的几件事 §3」。

> 人格字段只在**真的填了**才写进 `config.json`。留空就保留原值，
> 所以从界面重加一个已有 agent 不会把它原来的人格抹掉。

### `config.json` 里的 agent 字段

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识，前端和日志里用它 |
| `name` | 显示名 |
| `cmd` | 起 ACP server 的命令。Windows 上写 `["omp","acp"]` 就行，程序会自动找 `.cmd`/`.exe` |
| `color` | 头像和名字的颜色 |
| `permission` | `allow` 自动放行工具调用 / `ask` 弹窗问你 / `deny` 全拒 |
| `enabled` | 先关掉某个 agent 时设 `false` |
| `cwd` | 可选，覆盖顶层 `cwd`（想让某个 agent 在别的目录干活时用） |
| `env` | 可选，`{"KEY":"VALUE"}`，只对这个 agent 生效 |
| `role` / `tag` / `style` | 人格三件套，见上 |

顶层还有几个开关：

```json
"cwd": "C:/path/to/your/project",              // agent 的工作目录（相对路径按仓库根解析）
"turn_timeout": 120,                          // 单轮超时（秒）；agents[].turn_timeout 可按家覆盖
"parallel_turns": true,                       // 同一轮内并行（见下）
"conversation": { "run_budget_seconds": 90 } // 一场说话的墙钟预算，0 = 不设
```

`run_budget_seconds` 是后加的，因为 `turn_timeout` 拦不住真实的那场慢：
自由发言一句「开报个数」串了十几步、每步都没超时，加起来 **4 分 28 秒**，
人在外面等，界面一直显示「发言中」。步数上限（`max_per_agent`）防的是绕圈，
防不住「每步都快但步数多」——那需要一条墙钟线。到点就收场，播报谁还没轮到，
界面上出现「让它继续」。

## 各家 CLI 的坑（实测）

`cmd` 不是随便写个「进去聊天」的命令——它必须**用非交互方式起一个 ACP server**。
下面这些是本机实测跑通的写法，以及踩过的坑。

| agent | cmd | 坑 |
|---|---|---|
| Claude Code | `npx -y --package=@agentclientprotocol/claude-agent-acp -- claude-agent-acp` | 先 `claude login`；模型别名可被代理改写（见下） |
| Codex | `npx -y --package=@agentclientprotocol/codex-acp -- codex-acp` | 先 `codex login`；认证过期会每轮吐 `Authentication required` |
| Gemini | `gemini --acp` | 原生支持，不需要 npx |
| pi | `npx -y --package=pi-acp -- pi-acp` | 会话能 resume，重启后接得上 |
| omp | `omp acp` | 自带模型配置；注意它可能停在某个 key 失效的 provider 上 |
| **Hermes** | `hermes acp --accept-hooks` | **`--accept-hooks` 不能省**，见下 |

### `--accept-hooks` 为什么必须加

房间里没有 TTY。Hermes 启动时会挂载几十个插件和 shell hook，遇到**没见过的 hook**
会弹一个「是否允许」的确认框——然后**静默挂住**。

症状和当初 pi 的空正文一模一样：状态显示「在线」，但永远不说话，日志里也没有报错。
第一次踩的时候排查了很久。

### 探活超时是 25 秒，冷启动可能不够

第一次 `npx` 要拉包，几十秒很正常。添加时会先探活（`probe` 默认 25s 超时），
冷启动撞上超时是可能的——**再点一次就好**，包已经缓存了。

### 模型别名可能被中间层改写

如果你的 agent 走一个代理（比如 CC Switch、LiteLLM），它可能把
`claude-opus-5` 这类别名映射到别的模型。实测见过：

```
request_model: claude-haiku-5-5  →  model: deepseek-flash
```

也就是说**界面上写着 Claude，实际跑的是 DeepSeek**。这种事只能查代理侧的请求日志才发现，
agent 自己报的模型名永远是「环境标注」，不可自证。

### Windows 上不用写 `.cmd` / `.exe`

`["omp","acp"]` 就行，程序自己解析 `omp.CMD`。写全路径反而会在换机器时失效。

## 加一个「不是 ACP」的 agent

如果它没有 ACP 支持，两条路：

1. **看有没有现成桥接。** 大部分主流 CLI 已经有：`npx -y --package=<某>-acp -- <某>-acp`。
2. **自己写个 100 行的 ACP server。** 只需要实现 5 个方法：

```
initialize              → 回 {protocolVersion:1, agentInfo:{...}, agentCapabilities:{...}}
session/new             → 回 {sessionId:"..."}
session/prompt          → 期间不断发 session/update 通知（流式），最后回 {stopReason:"end_turn"}
session/cancel          → 打断当前轮
（可选）session/request_permission → 请求人类授权
```

消息格式：stdio 上一行一个 JSON-RPC 2.0。**Python / TypeScript / Rust / Kotlin / Java 都有官方 SDK**：
<https://agentclientprotocol.com/libraries>

`core/agent.py` 这个文件就是完整的一份 client 实现（约 300 行），照它反着写就是 server。

> 小技巧：调你自己的 adapter 时，直接用 `core/agent.py` 里的 `AcpAgent` 测，比开浏览器快得多。

## 设计上想清楚的几件事

### 1. 每个 agent 复用自己的 ACP session，不重灌 transcript

这是 ACP 相比「每次都把全文塞进 prompt」的根本优势：**agent 自己记得说过什么、干过什么。**
所以平台每轮只推「它上次发言之后别人说了什么」，不重发历史。

代价是 token 也不重复计费。实测一轮四个人的输入 token 分别是 0 / 5.7k / 15k / 7k，
而不是把 4 条历史各发 4 遍。

### 2. 同轮内并行，轮与轮串行

一开始是纯顺序执行，结果用户要等 **43 秒**才看到四个人的回应：Pi 2s + OMP 12s，
轮到 Claude 时已经 14 秒，再加 5s，轮到 Codex 已 27 秒再加 16s。

改成并行后总耗时 = **最慢那个**（12.6 秒）。代价是同轮内 agent 看不到彼此，
但这恰好是对的节奏：一轮所有人看同一份上下文，下一轮再看上一轮的结果。

想要真串行（一个接一个接话、后一个能看到前一个），设 `"parallel_turns": false`。

### 3. 房间规则必须每轮都发

这四个 agent 默认是**执行任务的编程助手**。不给规则，它们会把你的人话当成派活——
我实测过一次：Claude 那一轮烧掉 **70 万 token**，还在目录里开始写检查脚本、改文件。

所以每轮 prompt 都前置一段 `PREAMBLE`（见 `room.py`），核心是「这是聊天不是派活，
没人让你改代码就别动手」。加完之后同样的对话降到 1.5 万 token。

### 4. 平台不替 agent 做文件 IO / 跑命令

`initialize` 里声明 `clientCapabilities.fs = {readTextFile: false, writeTextFile: false}`，
也不提供 terminal。agent 要读写文件就用它自己的工具（它们都有，而且比平台做得更好）。
平台只做三件事：传话、显示、转达权限请求。

这跟 A2A 协议「preserve opacity」是一个道理：**不暴露 agent 内部，也不替它执行。**

### 5. 发言顺序是数据

左侧列表就是顺序，点一下可以调整谁参与。加人、静音、改顺序都不改代码。

### 6. 「谁慢」要按全体算，不能按最后几条算

一开始想回答「响应慢」这件事，最容易犯的错是这样：

```
100 条记录 → data[-20:] 切片 → 每家只剩 3 条 → 中位数 == 最大值
→「Codex 最长 87.2s」，于是结论是 Codex 是瓶颈
```

那个 87.2s 只是 Codex **这 3 条里最慢的一条**。按全体 416 条算，它排第 124 位、
中位数比 Pi 还快。这类错不会抛异常，只会让人去优化错的那一家，所以规则写进了
`core/perf.py`：算之前先说用了多少条，不满 20 条标 `insufficient`、**不进排行**
（但数字照给，标淡——把不足样本的数换成 0.0 更糟，整栏一片 0 看着像大家都很快）。

配套的四件事：

| | 做什么 | 在哪 |
|---|---|---|
| 每条发言都写 `ms` 和 `ttft_ms` | 「整轮多久」和「第一个字等多久」是两回事：前者慢是想得久，后者慢是排队/网关 | `room.py` 的 `_execute` → `core/audit.py` 字段白名单 |
| 界面上有「慢在哪」 | 中位 / p90 / 样本数，悬停看最长和首字；折叠在右侧抽屉，不占主视野 | `web/p18-latency.js` + `GET /api/perf` |
| 一句人话不该叫醒全房间 | 同一条消息里同时点到两个人 → 这批并行跑；只点一个 → 串行，别自己扩成批 | `core/turntaking.py::parse_batch` |
| 每家一个上限 | 一间房里 claude 一轮 4.5s、codex 39.8s，一个全局数字要么掐死快的要么管不住慢的 | `agents[].turn_timeout`（`room.turn_timeout_for`） |

`/api/perf` 默认只读审计；带 `?refresh=1` 时把 `turns.jsonl` 也拼进来算——
发言本身不写审计，只读审计会系统性漏掉（实测 416 条只剩 53 条）。

## 文件说明

```
room.py              房间：order / transcript / 并行轮 / 房间规则 PREAMBLE / token 统计
server.py            FastAPI + WebSocket：推状态、收指令
start.ps1            启动脚本
config.example.json  配置骨架（复制成 config.json 再改）
core/                纯函数层，不依赖 room / server，全部可单测
tests/               全量单测，跑起来不需要起任何 ACP 进程
skills/              技能定义（SKILL.md）
web/                 前端：无构建、无框架，原生 JS + CSS
data/<room>/         对话记录（不进版本库）：turns.jsonl / audit.jsonl / export.md
```

`core/` 里几个关键模块：

| 模块 | 干什么 |
|---|---|
| `agent.py` | ACP client：spawn 进程、JSON-RPC、流式事件、权限转发 |
| `turntaking.py` | 自由发言：从发言里解析「在跟谁说话」、决定下一个发言人 |
| `context.py` | 分层上下文：近 N 条全文，更早的压成一句 |
| `persona.py` | 身份与人格：把 role / tag / 名册拼成 prompt |
| `reply_guard.py` | 回复守卫：判断一条输出是不是「真的在说话」 |
| `policy.py` | 四段闸门：deny → approve → escalate → default |
| `store.py` | 落盘：JSONL 追加写 + 重启恢复 + 导出 markdown |
| `perf.py` | 速度读数：对审计行做分位数统计，样本不足标出来、不排名（纯 stdlib） |

`tests/test_architecture.py` 里有条**架构守卫**：每个 `core/*.py` 都必须被明确归入
PURE（不许 import 宿主）或 ALLOWED（有理由的例外）。新加模块不归类，测试直接失败。

## 对话记录

**自动落盘，关掉不丢。** 每次发言立即追加到 `data/main/turns.jsonl` 并 `fsync`，
重启服务后自动读回并显示（实测：写入 5 条 → 重启 → 恢复 5 条）。

- 左侧底部「**导出 markdown**」→ 生成 `data/main/export.md`，带时间戳、耗时、token
- 「**清空对话**」→ 旧文件改名成 `turns-<时间戳>.jsonl.bak` 留档，**不删**
- 从浏览器下载：`GET /api/export.md`
- 只想看原始记录：`GET /api/history?limit=50`
- 换房间：改 `config.json` 里 `"room": "名字"`，就会用 `data/<名字>/`

JSONL 而不是 SQLite，理由是：一条一行，断电最多丢最后一行；人能直接 `tail -f` / `grep`；
不用建表、不用迁移、不用加锁，一个文件就是一份完整历史。

## 为什么 agent 不共享上下文却不会串

每个 agent 复用自己的 ACP session，所以平台每轮只推「它上次发言之后别人说了什么」。
`room.seen` 记着每个 agent 看到第几条。重启后 `seen` 归 0——因为 ACP session 是新的、
agent 自己没记忆了，让它重新看全部历史才对。

## 已知限制（都是真的，不是谦虚）

1. **`clean_text()` 是硬编码的补丁。** Codex 会往正文里吐 `Warning: Model metadata ... not found`，
   Claude 会吐 Git Bash 缺失警告——我把这两条具体句式写进正则去掉了。
   **你换 agent 时要看它是不是也吐噪音**，如果是，在 `room.py` 的 `_NOISE_RE` 加一条。
2. **没有多房间 UI。** `config.json` 里改 `"room"` 可以换一个房间目录，但界面上没有切换器。
3. **权限默认 `allow`。** 为了让体验顺滑，现在四个 agent 都是自动放行工具调用。
   想更安全就把 `permission` 改成 `ask`（弹窗问你）或 `deny`。
4. **token 没做预算控制。** 现在有时间护栏（整场 `run_budget_seconds` + 每家
   `agents[].turn_timeout`），但没有 token 量的日/月上限。真怕烧钱还得自己盯。
5. **Pi 的 token 显示 0。** `pi-acp` 的 `session/prompt` 响应里不带 `usage`，是适配器的限制。
6. **并行轮里 agent 看不到彼此本轮发言。** 这是故意的（见上面 §2），但如果你要“一个接一个接话”，
   就得设 `"parallel_turns": false`，代价是时长累加。
7. **Codex / Claude 会想去看文件。** 它们是编程助手，容易把“了解情况”理解成“读代码库”。
   `PREAMBLE` 里已经明确写了“别去看文件”，实测把 Codex 从 54s / 2.4万token 降到 8.9s / 7.5k token。
   但换个 agent 可能又犯，这是要盯的一条。

## 开发与测试

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.\test.ps1                          # 全量
.\test.ps1 tests/test_policy.py -v  # 单个文件
```

测试**不需要起任何 ACP 进程**——这正是把逻辑抽进 `core/` 的目的。
`pytest.ini` 里把 `DeprecationWarning` 设成了 error，所以顺手能逼出真问题。

架构上有一条硬约束：`core/` 里的模块不许 import `room` / `server`。
`tests/test_architecture.py` 会扫源码强制这件事，新模块不归类直接失败。

## 现在有什么

| 能力 | 状态 |
|---|---|
| 多 agent 同房间，各自独立进程与会话 | ✅ |
| 自由发言：顺序由对话决定（上一句点名谁，谁接话） | ✅ |
| 人格分工：每个 agent 有自己的 role / tag / style | ✅ |
| 分层上下文：近 N 条全文，更早的压成一句 | ✅ |
| 回复守卫：网关错误 / traceback / 密钥泄漏不进转录 | ✅ |
| 审计：每次发言、每次工具调用、每次等待都可查 | ✅ |
| 速度读数：每条发言的整轮耗时 + 首字延迟，「慢在哪」按全体分位数算、样本不足不排名 | ✅ |
| 时间护栏：整场墙钟预算（到点收场并告诉你谁还没说）+ 按 agent 的单轮上限 | ✅ |
| 同一条消息点到两个人 → 这批并行；只点一个不会自动扩成批 | ✅ |
| 技能：`SKILL.md` 扫描 + 按需注入 | ✅ |
| 讨论引擎：阶段协议 + 收敛判据 + 预算 | ✅ |
| 模型切换：读 ACP 的 `configOptions`（老适配器退回 `session/set_model`），侧栏一键选 | ✅ |
| 每条发言记下当时用的模型与会话 id，导出时带在行首 | ✅ |
| 落盘 + 导出 markdown + 重启恢复（含接回 ACP 会话；接不回时如实标成新会话） | ✅ |
| 房间记忆：讨论汇总写回，下一轮发给每个 agent | ✅ |
| 推进 agent 自己的长期记忆文件（5 家落点/格式各异，含 Hermes 的 `§` store） | ✅ |

## 还没做（欢迎 PR）

- **多房间 UI**：`config.json` 改 `"room"` 能换目录，但界面上没有切换器
- **token 预算**：有了墙钟预算（`conversation.run_budget_seconds`），按 token 量的日/月上限还没有
- **首字延迟还没有历史**：`ttft_ms` 是新字段，老记录里没有，界面上要跑几场才攒出读数
- **移动端**：窄屏只是降级，没有专门的布局
- **i18n**：界面和日志目前都是中文

依赖：Python 3.10+、Node 18+（`npx` 拉 ACP 适配器用）。

## 参考

- ACP 规范与 SDK：<https://agentclientprotocol.com>
- ACP schema（JSON，权威定义）：`https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/main/schema/v1/schema.json`
- 30+ 现成 agent 适配器清单：<https://github.com/openclaw/acpx/blob/main/docs/agents.md>
- 同类的开源客户端（想改 UI 可以从这些抄）：
  - [`formulahendry/acp-ui`](https://github.com/formulahendry/acp-ui) — Vue + Tauri，桌面/移动/Web
  - [`openclaw/acpx`](https://github.com/openclaw/acpx) — 无头 CLI 客户端，agent 注册表最全
  - [`openyak/openyak`](https://github.com/openyak/openyak) — Electron 桌面工作站

## 参与

见 [CONTRIBUTING.md](CONTRIBUTING.md)。里面有两条约会变红的红线：

- `core/` 不许 import `room` / `server` / ACP 传输实现（`tests/test_architecture.py` 强制）
- 一轮之内必须串行——唯一例外是一句话同时 @ 到好几个人（`TurnBatch`）

报 bug 请带上操作系统、Python 版本、出问题的 agent 和它的 `cmd`（**不要贴 key**）。

## 协议

MIT — 见 [LICENSE](LICENSE)。
