---
name: audit-review
description: 当你需要回答「这段时间平台到底做了什么、有没有人在看、哪一步被拒绝了」时使用——规定怎么读 audit.jsonl 与 turns.jsonl 找异常，而不是通读日志。
---

# audit-review — 审计怎么读

## 什么时候用

- 有人问"昨天那轮为什么失败了"
- 怀疑某个 agent 做了没被授权的事
- 定期检查：谁在没人看着的时候跑了任务
- 交接/定时任务没有结果，要查在哪断的

## 不要怎么做

**不要 `cat` 全量 jsonl**。一次讨论就上千行，通读等于没读。

## 正确顺序（四步，从窄到宽）

### 1. 先定位时间窗
```powershell
# 只看最近 X 分钟
Get-Content audit.jsonl -Tail 200 | Select-String '"ts": 17936'
```
`ts` 是 epoch 秒。先框出时间，再看内容。

### 2. 再按"异常状态"过滤，而不是按 agent
```powershell
# 所有失败与被拒的工具调用
Get-Content audit.jsonl | Select-String '"status": "(error|denied|refused)"'
```

### 3. 只看决策，不看正文
审计行里**最有用的是 `decision` 和 `rule` 两列**，不是 `title`：
```powershell
Select-String -Path audit.jsonl -Pattern 'decision":"(deny|escalate|auto_allow)' | Group-Object { ($_ -split '"decision":"')[1].Split('"')[0] } | Select-Object Count, Name
```
计数本身就是结论：如果 100% 是 `auto_allow`，说明**这个平台没有闸门**。

### 4. 最后才回 turns.jsonl 对时间
用 `turn_seq` 把审计行和发言对上。看那一条发言的 `status` 是否 `ok`。

## 三个必查项

| 查什么 | 怎么查 | 说明 |
|---|---|---|
| **有没有 initiator** | `Select-String '"initiator"' audit.jsonl` | 没有这个字段，你就答不出"当时有没有人在看" |
| **拒绝的行** | `decision != auto_allow` | 交接被拒的审计行**最重要**——它不在 transcript 里，只在审计里 |
| **静默失败** | `tool_call` 有、对应 `tool_result` 没有 | 说明进程中断，不是模型拒绝 |

## 输出格式

```
时间窗：<from> ~ <to>
总行数：<n>（auto_allow <a> / deny <d> / escalate <e>）
异常：<最多 3 条，文件:行号 + 一句话>
结论：<一句话回答提问>
```

## 一条纪律

**审计里没写的事，等于没发生。**
如果某个动作没有审计行，不要说"应该没做"——要说"没有记录，无法确认"。
