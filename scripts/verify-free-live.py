"""实测自由发言：是不是真的"接着某人的话反驳"，而不是各说一段。

校验的不是"有几个人说话"，而是：
  1. 同一个人的名字出现在**别人**的话里（说明在回应具体的人）
  2. 相邻两条形成"被引用的那个人的发言 → 反驳它的人"的顺序
  3. 有反驳标记（反对/说反了/不对/但是…）
  4. 服务没回归：跑完 running=False，还能再发一条
"""

import json
import re
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8770"
NAMES = {"pi": "Pi", "omp": "OMP", "claude": "Claude", "codex": "Codex"}
REBUT = re.compile(r"反对|说反了|不对|但是|恰恰|反过来|问题在场|不同意|站不住|相反|有问题")


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=20))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def main():
    s0 = get("/api/state")
    before = s0["count"]
    print(f"[起] running={s0['running']} count={before}")

    q = ("@claude 一句话回答：房间里谁最适合先开口？"
         "给出你的判断和理由，不要客气。")
    print(f"[发] {q}")
    print(" ", post("/api/say", {"text": q, "auto": True}))

    deadline = time.time() + 420
    last = before
    while time.time() < deadline:
        time.sleep(6)
        s = get("/api/state")
        if s["count"] != last:
            print(f"  ... {s['count']} 条")
            last = s["count"]
        if not s["running"] and s["count"] > before:
            break

    s = get("/api/state")
    turns = [t for t in s["turns"] if t["seq"] > before and t["status"] != "human"]
    print(f"\n[果] running={s['running']} 新增 {len(turns)} 条")

    for t in turns:
        body = (t.get("text") or "").replace("\n", " ")
        print(f"  {t['seq']:>3} {t['speaker']:<7} {t['status']:<6} | {body[:100]}")

    # ---- 1. 别人名字被提到 ----
    mentions = []
    for t in turns:
        body = t.get("text") or ""
        others = [aid for aid in NAMES
                  if aid != t["speaker"] and re.search(NAMES[aid], body, re.I)]
        if others:
            mentions.append((t["seq"], t["speaker"], others))
    print(f"\n[1] 提到别人名字的发言：{len(mentions)} 条")
    for m in mentions[:6]:
        print("    ", m)

    # ---- 2. 相邻形成"回应" ----
    chains = []
    for i in range(1, len(turns)):
        prev, cur = turns[i - 1], turns[i]
        body = cur.get("text") or ""
        if re.search(NAMES.get(prev["speaker"], "\0"), body, re.I):
            chains.append((prev["seq"], prev["speaker"], cur["seq"], cur["speaker"]))
    print(f"[2] 相邻且点名上一句说话人的链：{len(chains)} 条")
    for c in chains[:6]:
        print("    ", c)

    # ---- 3. 反驳标记 ----
    reb = [(t["seq"], t["speaker"], REBUT.search(t.get("text") or "").group())
           for t in turns if REBUT.search(t.get("text") or "")]
    print(f"[3] 带反驳标记的发言：{len(reb)} 条")
    for r in reb[:6]:
        print("    ", r)

    # ---- 4. 服务无回归 ----
    after = get("/api/state")
    ok2 = post("/api/say", {"text": "ping", "auto": False})
    print(f"\n[4] 结束后 running={after['running']}；再发一条 → {ok2}")

    ok = (len(turns) >= 2 and len(chains) >= 1 and len(reb) >= 1
          and after["running"] is False and ok2.get("ok"))
    print(f"\n结论：{'通过 —— 有链式反驳，服务正常' if ok else '不达标'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
