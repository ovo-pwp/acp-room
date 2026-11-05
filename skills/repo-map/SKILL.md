---
name: repo-map
description: 当你要进入一个没读过的代码库、或要在其中定位某个功能时使用——先建目录-职责地图再动手，禁止边猜边改。用于接手新仓库、给 agent 派活前的侦察。
---

# repo-map — 进仓先建图

## 为什么

在一个没读过的仓库里，**最大成本不是写代码，是找错文件**。
找错文件 → 改错层 → 测试失败 → 再猜一遍，一轮就是半小时。

先花 10 分钟建图，能省几小时。

## 五步（按顺序，每步产出，不要跳）

### 1. 读入口清单（不看正文）
```powershell
Get-ChildItem -File | Select-Object Name            # README / package.json / pyproject.toml / Makefile
```
只记：**语言、包管理器、怎么跑、怎么测**。

### 2. 列目录骨架，标职责（不看文件内容）
```powershell
Get-ChildItem -Directory | ForEach-Object { "{0,-24} {1}" -f $_.Name, (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue).Count }
```
输出一张 `目录 → 职责 → 文件数` 的表。**文件数 > 500 的目录一定是你用不上的（node_modules / vendor / dist），标掉。**

### 3. 找"边界文件"
只找这三类，其他一概不读：
- **协议/契约**：schema、proto、types、`*_client`、`*_server`
- **入口**：`main.*`、`index.*`、`app.*`、路由注册
- **配置**：config、`.env.example`、yaml

### 4. 找"闸门"
任何系统都有"决定能不能做"的地方。搜这些词：

```powershell
Select-String -Path **/*.ts,**/*.py -Pattern 'permission|policy|authorize|guard|allow|deny' -List
```
**找到闸门在哪，就找到了这个系统的设计者最在意什么。**

### 5. 写图（离开时留下）

```markdown
# <repo> 地图
- 语言/包管理：__
- 跑：__  测：__
- 目录职责：
  | 目录 | 职责 | 可信度 |
- 边界文件（读这 3~5 个就够）：
- 闸门在：__
- 没读懂的地方（明写）：__
```

## 三条纪律

1. **不要读 node_modules / vendor / dist**。读它们的成本是 100 倍，收益是 0。
2. **不要相信 README 里的架构图**，它常常落后于代码。图里写了什么，去文件里验一下再抄。
3. **"没读懂"要明写**。图上留下一个诚实的洞，比画一条假线有用。

## 给另一个 agent 派活时

把这张图作为交接的 `bounds` 一起给出去，比让它自己再侦察一遍省一次完整 run。
