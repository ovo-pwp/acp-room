---
name: acp-agent-debug
description: 当 ACP agent 连不上、不回话、超时、协议报错或权限请求卡住时使用——按固定顺序排查，避免瞎猜。适用 codex-acp / claude-agent-acp / pi-acp / omp acp 等所有 ACP 适配器。
---

# acp-agent-debug — ACP 故障排查序

## 排查顺序（不许跳步，每步都要有输出）

### 1. 进程起来了吗
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'codex-acp|claude-agent-acp|pi-acp|omp' } |
  Select-Object ProcessId, @{n='cmd';e={$_.CommandLine.Substring(0,[Math]::Min(90,$_.CommandLine.Length))}}
```
没进程 → 问题在启动（命令不存在 / npx 拉包失败 / 认证没过），**不要看协议层**。

### 2. stdio 是活的吗
ACP 走 stdin/stdout 上的 JSON-RPC。**任何非 JSON 的东西写进 stdout 都会毁掉协议。**

```powershell
# 先单独跑一次，看它往 stdout 吐了什么
npx -y --package=@agentclientprotocol/codex-acp -- codex-acp
```
如果第一行不是 JSON-RPC（比如是 banner、进度条、日志），**这就是根因**。
解决办法：适配器加日志开关写 stderr；宿主侧把 stdout 按行解析，非 JSON 行丢到日志而不中断。

### 3. `initialize` 握手过了吗
必看返回的 `agentInfo`。**空 `agentInfo` = 适配器版本不对或启动脚本是假的。**

### 4. `session/new` 有 `sessionId` 吗
返回里还应带 `models` / `configOptions`。**没有 `models` 说明这个适配器不支持模型切换**，别去调 `session/set_model`。

### 5. 是超时还是报错
区分三件事，处理方式完全不同：

| 现象 | 含义 | 处理 |
|---|---|---|
| 有 `tool_call` 审计行、没有对应 `tool_result` | 进程中断/被杀 | 查孤儿进程、内存、`turn_timeout` |
| 有 `session/request_permission` 发出但宿主没回 | 宿主侧审批卡住 | 检查审批通道是否在等一个永远不会点的人 |
| 什么都没有，`prompt` 也不返回 | 模型端慢/网络 | 加长 timeout，先做一次最小 prompt 验证 |

### 6. 权限请求卡在哪一层
**这是最容易误判的一步。** 记住三层的区别：

- **宿主 `session/request_permission` 拒绝** → agent 收到拒绝，会自己换说法或放弃。审计要有 `decision=deny`。
- **agent 侧工具错误** → `tool_result.status=error`。agent 会说"写入被拒"，但**平台从未参与决策**。
- **配置里某字段** → 先证明它真的生效，别假设。

判据：**审计里有没有 `decision != auto_allow` 的行。没有，就说明平台没拦，是 agent 自己拦的。**

## 四条已知的坑

1. **`plan` 模式不是安全边界**。某些 agent 会自己 `switch_mode` 退出 plan 再执行。
2. **token 计数是会话累计值**，不是单轮值。直接相加会双计（见过 12 条发言报 142k）。
3. **孤儿进程**。`npx` 起的子进程不会跟着父进程死。关服务后必查。
4. **stdout 污染**。适配器打印任何东西都会挂。

## 输出格式

```
层：<进程 / stdio / initialize / session / prompt / permission>
证据：<命令 + 原始输出前 5 行>
根因：<一句话>
下一步：<一条可执行命令>
```
