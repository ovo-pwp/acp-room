# 参与开发

先说清楚这个项目**是什么、不是什么**，省得白费力气：

- ✅ 它是一个**房间**：负责把多个已经装好的 ACP agent 放进同一场对话，管发言顺序、上下文、审计、权限。
- ❌ 它不是一个 agent 框架。这里没有 prompt 编排、没有工具定义、没有链式调用。agent 是你机器上的独立进程，你自己的代码只有 `room.py` / `core/`。

所以「让 agent 更聪明」不是这个项目的活；「让几个 agent 好好说话」才是。

---

## 环境

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt    # Windows: .venv\Scripts\pip

./test.sh          # Windows: .\test.ps1
./start.sh         # Windows: .\start.ps1
```

跑起来需要 `config.json`：`cp config.example.json config.json`。

**Python 3.10+。** 测试不需要任何 ACP 进程——这是分层的意义，不是巧合。

---

## 一条硬规则：`core/` 不许 import 宿主

```
core/*.py   ✗ import room     ✗ import server     ✗ 任何 ACP 传输实现
```

`tests/test_architecture.py` 会扫源码强制执行，违反直接红。理由很实际：

> 一个函数只要碰了 `room` 或 ACP 进程，想验证它就必须起 4 个子进程、等几十秒、还要碰运气。
> 那种测试不会有人写，写了也不会有人跑。

所以**能抽成纯函数的逻辑一律抽走**。已经抽出去的部分在 `core/`：
`turntaking`（谁接话）、`context`（上下文分层）、`policy`（权限四段闸门）、
`persona`（人格拼装）、`convergence`（收敛判据）、`reply_guard`（回复守卫）、`perf`（耗时统计）。

### 加了新的 `core/` 模块怎么办

必须在 `tests/test_architecture.py` 里**明确归类**，否则测试失败：

- `PURE` —— 完全脱离宿主，可以单测
- `ALLOWED` —— 确实必须依赖宿主（比如要复用 ACP 客户端），**得写明理由**

每往 `ALLOWED` 里加一个，就少一块能被单测保护的区域。所以默认答案永远是 `PURE`。

### 如果一条测试开始需要 mock ACP

**不要加 mock。** 那是 `core/` 边界被破了的信号，回去把逻辑抽出去。

`tests/test_room_seams.py` 里写着这条规则。它是全仓库**唯一** `import room` 的测试文件，
刻意做得极薄：只验证「room 把什么交给了 core」，不验证 core 内部。

---

## 测试

```bash
./test.sh                        # 全量
./test.sh tests/test_policy.py -v
```

- **不要起 ACP 进程。** 需要真进程的只有 `tests/test_session_resume.py`，它自己管。
- `pytest.ini` 把 `DeprecationWarning` 设成 error——这是有意的，顺手逼出真问题。
- 写测试时先想「这个 bug 当时是什么症状」，把症状写进 docstring。半年后有人改坏它，能立刻看懂为什么会有这条测试。

---

## 提交

主题行用祈使句，正文解释**为什么**，不是做了什么。例：

```
Add reply guard for gateway errors

omp returned 402/401 bodies that got recorded as normal turns. Worse, other
agents then quoted them as if they were content — the transcript showed
claude and pi debating an error message across several rounds.
```

改了行为就补测试。修了 bug 就试着写出会失败的那个用例——写不出来，说明还没定位到根因。

---

## 欢迎的 PR

README 的「还没做」一节是准的，那里列的都是真没做的：

- 多房间 UI
- token 预算（已有墙钟预算 `conversation.run_budget_seconds`，按 token 计的日/月上限还没有）
- 跨 agent 的共享记忆
- 移动端布局
- 界面 i18n（现在界面和日志都是中文）

也比较欢迎：

- **新的 ACP adapter 适配经验**——哪家 CLI 需要什么特殊参数（比如 Hermes 的 `--accept-hooks`）、
  哪种模型配置会踩坑。这类经验写进 README 的「各家 CLI 的坑」比写代码更有价值。
- **`scripts/` 里的验收工具**——布局审计、样式测量、链式反驳检查。

不太会合入的：把 agent 编排逻辑塞进这个项目、引入前端构建链、为了「快」把串行改成并行
（见下）。

---

## 两条别踩的设计红线

**1. 一轮之内必须串行。** 后说话的人要能看到前面的人说了什么，这是「接着某人的话反驳」
能发生的前提。并行化会静默破坏它——表面上还是四个人在说话，实际上已经不是对话了。
`tests/test_room_seams.py::test_自由发言默认串行而不是并行` 盯着这件事，它还做过变异测试。

唯一的例外是**同一句话同时 @ 到好几个人**（`TurnBatch`）：他们看的上文是同一份、互相没有依赖，
并行才有意义。

**2. 被拦下的内容不进转录。** 空正文、网关错误、traceback 都不该写进 `turns.jsonl`。
一旦进去，它就会被别的 agent 当成「某个人说过的话」反复引用。
状态用 `wait` / `empty` / `blocked` 区分，审计里留痕，转录里不留。

---

## 报 Bug

请带上：

- 操作系统 + Python 版本
- 哪个 agent（`claude` / `codex` / `pi` / `omp` / `hermes` / 你加的），以及它自己的版本
- `config.json` 里那个 agent 的 `cmd`（**不要贴 key**）
- 日志：`data/<room>/audit.jsonl` 的相关几行，或者启动时的 stderr

⚠️ **贴日志前先检查有没有密钥。** 常见位置：`agents[].env`、各家 CLI 的模型配置、
代理返回的错误正文。把 `sk-` 开头的串替换成 `sk-***`。

界面问题请附截图，并说明窗口宽度——布局是按 1400 / 1100 / 900 三档验的。

---

## For non-Chinese contributors

The code comments and most docs are Chinese, but everything structural is simple:

1. `core/` must not import `room`, `server`, or any ACP transport. `tests/test_architecture.py`
   enforces it — classify any new `core/` module as `PURE` or `ALLOWED` (with a reason).
2. Tests must not spawn ACP processes. If a test needs to mock ACP, the logic belongs in `core/`.
3. Run `./test.sh` (or `.\test.ps1` on Windows) before opening a PR. No external services needed.
4. Within a turn, agents are strictly serial — later speakers must see earlier ones. The only
   exception is a single message mentioning several agents (`TurnBatch`), which may run in parallel.
5. Blocked replies (empty body, gateway errors, tracebacks) never enter the transcript.

Feel free to open issues in English; replies may be slow but they will be answered.
