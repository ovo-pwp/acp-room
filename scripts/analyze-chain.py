"""从已存的 turns.jsonl 里判定：有没有"接着某人的话反驳"。

修正上一版的两个错：
  1. 名字匹配没加词界 —— `Pi` 大小写不敏感地命中了 "api key" 里的 pi，
     于是把 omp 的 401 报错当成"在跟 pi 说话"。
  2. 只看名字不够 —— 真正优质的反驳常常**不点名**，而是引用对方那句话
     （"'然后就停下'是这条建议里唯一有问题的部分"）。所以还要看
     "引用了上一句里的片段"。
"""

import json
import re
import sys

NAMES = {"pi": "Pi", "omp": "OMP", "claude": "Claude", "codex": "Codex"}
REBUT = re.compile(r"反对|说反了|不对|但是|恰恰|反过来|有问题|不同意|站不住|相反|唯一|误判|堵住")
# 指代上一句：反驳很少原文照抄，更常见的是"这条建议…"这种回指
ANAPHOR = re.compile(r"这条建议|这个建议|这条|这个方案|你说的|他说|对方|上一条|这建议|这条判据|你的")
# 报错/故障文本不算发言内容
BROKEN = re.compile(r"^\s*(401|402|⚠|Insufficient|Authentication|ACP 错误)")


def name_re(aid):
    n = NAMES[aid]
    # 词界：两边都不能紧挨字母数字，避免 "api" 命中 "Pi"
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", re.I)


def phrases(text, n=6):
    """取几句有区分度的片段，用来判断"有没有引用上一句"。"""
    parts = [p.strip() for p in re.split(r"[，。；：！？\n]", text) if len(p.strip()) >= n]
    return parts[:12]


def main(path="data/main/turns.jsonl", start_seq=0):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r.get("seq", 0) > start_seq]
    # 人的发言不是"agent 之间的接话"，排除；故障文本也不算发言内容
    rows = [r for r in rows
            if r.get("speaker") != "human" and not BROKEN.search(r.get("text") or "")]

    print(f"参与分析的发言：{len(rows)} 条")
    for r in rows:
        print(f"  {r['seq']:>3} {r['speaker']:<7} | {(r.get('text') or '')[:90]}")

    print("\n--- 相邻回应判定 ---")
    chains = 0
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        pt, ct = prev.get("text") or "", cur.get("text") or ""

        named = bool(name_re(prev["speaker"]).search(ct))
        # 引用：当前这句里出现了上一句的某个片段（≥6 字，足够特别）
        quoted = [p for p in phrases(pt, 6) if p in ct]
        anaph = ANAPHOR.search(ct)
        reb = REBUT.search(ct)

        refs, kinds = [], []
        if named:
            refs.append("name"); kinds.append(f"点名 {prev['speaker']}")
        if quoted:
            refs.append("quote"); kinds.append(f"引用「{quoted[0][:18]}」")
        if anaph:
            refs.append("anaphor"); kinds.append(f"回指「{anaph.group()}」")
        if reb:
            kinds.append(f"反驳词「{reb.group()}」")

        # 链 = 相邻 + 指向上一句（点名/引用/回指）+ 有反驳语气
        if refs and reb:
            chains += 1
            print(f"  ✓ {prev['seq']} {prev['speaker']} → {cur['seq']} {cur['speaker']}: "
                  f"{'、'.join(kinds)}")
        elif refs or reb:
            print(f"  · {prev['seq']} {prev['speaker']} → {cur['seq']} {cur['speaker']}: "
                  f"{'、'.join(kinds) or '无指向'}")

    print(f"\n链式反驳：{chains} 处")
    return 0 if chains >= 1 else 1


if __name__ == "__main__":
    sys.exit(main(start_seq=int(sys.argv[1]) if len(sys.argv) > 1 else 0))
