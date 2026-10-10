"""长期记忆：把对话压缩成"她记得的事"，按天累积，并沉淀关于他的持久事实。

三层记忆：
  1. 短期（persona.py）：最近 18 轮原始对话
  2. 长期（本模块）：每日摘要 + 持久事实 → 注入 system prompt
  3. 日记（diary.py）：她每天写的东西，同时喂进长期记忆

存储：out/memory.json
  {
    "days":  [{"date":"2026-10-10","day":1,"summary":"...","events":[...]}],
    "facts": [{"t":..., "text":"他打游戏爱用机械键盘"}],
    "cursor": 27          # 已经摘要到 session.jsonl 的第几行（增量）
  }
用法：
  run.cmd memory.py                # 看看现在的记忆
  run.cmd memory.py --update       # 立刻压缩尚未摘要的对话
  run.cmd memory.py --forget 3     # 删掉最近 3 条事实（纠错用）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, r"D:\DHS\voice")

OUT = r"D:\DHS\voice\out"
STORE = os.path.join(OUT, "memory.json")
TALK_LOG = os.path.join(OUT, "session.jsonl")
DIARY_DIR = os.path.join(OUT, "diary")
CONFIG = r"D:\DHS\.wxwork\bot_config.json"
MAX_FACTS = 40          # 事实最多留这么多条，超出丢最旧的
MIN_NEW_TURNS = 6       # 攒够这么多轮才值得压缩一次


def _load_talk():
    rows = []
    if not os.path.exists(TALK_LOG):
        return rows
    for line in open(TALK_LOG, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        u, h = (j.get("user") or "").strip(), (j.get("her") or "").strip()
        if u and h:
            rows.append((j.get("t") or 0, u, h))
    return rows


class Memory:
    def __init__(self, path=STORE):
        self.path = path
        try:
            self.data = json.load(open(path, encoding="utf-8"))
        except Exception:
            self.data = {"days": [], "facts": [], "cursor": 0}
        self.data.setdefault("days", [])
        self.data.setdefault("facts", [])
        self.data.setdefault("cursor", 0)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            json.dump(self.data, open(self.path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ---------- 注入 ----------
    def context(self, max_days=10, max_facts=18):
        """生成给模型看的"你还记得的事"。"""
        parts = []
        days = self.data["days"][-max_days:]
        if days:
            parts.append("【你还记得的这些天】（越靠下越近，越近的事记得越清楚）")
            for d in days:
                s = (d.get("summary") or "").strip()
                if s:
                    parts.append(f"- 第{d.get('day', '?')}天（{d.get('date', '')}）：{s}")
        facts = self.data["facts"][-max_facts:]
        if facts:
            parts.append("【关于他，你一直记着的】（这些是你确定知道的，别记错）")
            for f in facts:
                t = (f.get("text") or "").strip()
                if t:
                    parts.append(f"- {t}")
        return ("\n" + "\n".join(parts) + "\n") if parts else ""

    # ---------- 更新 ----------
    def pending(self):
        rows = _load_talk()
        return rows[self.data["cursor"]:]

    def update(self, llm=None, force=False):
        """把尚未摘要的对话压缩进长期记忆。返回是否有更新。"""
        rows = self.pending()
        if not rows:
            return False
        if len(rows) < MIN_NEW_TURNS and not force:
            return False
        if llm is None:
            from gateway import Llm
            llm = Llm(json.load(open(CONFIG, encoding="utf-8")))
        text = "\n".join(f"他：{u}\n她：{h}" for _, u, h in rows[-60:])
        today = datetime.fromtimestamp(rows[-1][0] or time.time()).strftime("%Y-%m-%d")
        sys_p = (
            "你在替{NAME}整理长期记忆。把下面这段对话压缩成她自己以后能想起来的东西。\n"
            "只输出 JSON，格式：\n"
            '{"summary":"今天发生的、她该记住的（40~80字，她的第一人称口吻，'
            '只写事实和她的感受，不要写称呼和客套）",'
            '"facts":["关于他的、以后也成立的持久事实，比如他的喜好/习惯/近况；'
            '没有新的就留空数组，最多3条，每条15字以内"]}\n'
            "要求：summary 不要复述原话，是回忆不是转述；facts 只写长期有效的，"
            "一次性的小事不要写进去。"
        )
        try:
            raw = llm.complete([{"role": "system", "content": sys_p},
                                {"role": "user", "content": text}],
                               model=getattr(llm, "fast_model", None) or "deepseek-chat",
                               temperature=0.8, timeout=90)
        except Exception as e:
            print(f"  [记忆] 压缩失败 {type(e).__name__} {str(e)[:60]}")
            return False
        from gateway import first_json
        j = first_json(raw)
        summary = (j.get("summary") or "").strip()
        # 天数（沿用 diary 的算法）
        day_no = self._day_no(today)
        if summary:
            self.data["days"] = [d for d in self.data["days"] if d.get("date") != today]
            self.data["days"].append({"date": today, "day": day_no, "summary": summary,
                                      "t": time.time(), "turns": len(rows)})
            self.data["days"] = self.data["days"][-120:]
        for f in (j.get("facts") or []):
            f = (f or "").strip()
            if f and not any(f == (x.get("text") or "") for x in self.data["facts"]):
                self.data["facts"].append({"t": time.time(), "text": f})
        self.data["facts"] = self.data["facts"][-MAX_FACTS:]
        self.data["cursor"] = len(_load_talk())
        self.save()
        print(f"  [记忆] 已记住第{day_no}天：{summary[:50]}  "
              f"新事实 {len(j.get('facts') or [])} 条")
        return True

    def _day_no(self, date_str):
        try:
            st = json.load(open(os.path.join(OUT, "diary_state.json"), encoding="utf-8"))
            d0 = datetime.strptime(st["start_date"], "%Y-%m-%d").date()
            d1 = datetime.strptime(date_str, "%Y-%m-%d").date()
            return (d1 - d0).days + 1
        except Exception:
            return 1

    # ---------- 日记并入 ----------
    def absorb_diaries(self):
        """把日记里还没进记忆的部分也记下（她是自己写的东西，记得最牢）。"""
        if not os.path.isdir(DIARY_DIR):
            return
        have = {d.get("date") for d in self.data["days"]}
        changed = False
        for name in sorted(os.listdir(DIARY_DIR)):
            if not name.endswith(".md"):
                continue
            date_str = name[:-3]
            if date_str in have:
                continue
            try:
                body = open(os.path.join(DIARY_DIR, name), encoding="utf-8").read()
            except Exception:
                continue
            # 取正文（去掉标题、天数行、图片行）
            lines = [l.strip() for l in body.splitlines()
                     if l.strip() and not l.startswith("#") and not l.startswith("![")
                     and not l.startswith("第 ")]
            text = "".join(lines)
            if not text:
                continue
            self.data["days"].append({"date": date_str, "day": self._day_no(date_str),
                                      "summary": text[:160], "t": time.time(),
                                      "from": "diary"})
            changed = True
        if changed:
            self.data["days"].sort(key=lambda d: d.get("t") or 0)
            self.save()

    def show(self):
        print(f"=== 长期记忆 {self.path} ===")
        print(f"cursor={self.data['cursor']}  天={len(self.data['days'])}  "
              f"事实={len(self.data['facts'])}  待摘要轮次={len(self.pending())}")
        for d in self.data["days"][-12:]:
            tag = "（来自日记）" if d.get("from") == "diary" else ""
            print(f"  第{d.get('day')}天 {d.get('date')}{tag}: {(d.get('summary') or '')[:64]}")
        if self.data["facts"]:
            print("  关于他：")
            for f in self.data["facts"][-12:]:
                print(f"    - {f.get('text')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="立刻压缩未摘要的对话")
    ap.add_argument("--absorb", action="store_true", help="把日记并进长期记忆")
    ap.add_argument("--forget", type=int, default=0, help="删掉最近 N 条事实")
    a = ap.parse_args()
    m = Memory()
    if a.absorb:
        m.absorb_diaries()
        print("已并入日记")
    if a.update:
        m.update(force=True)
    if a.forget:
        m.data["facts"] = m.data["facts"][:-a.forget]
        m.save()
        print(f"已删掉 {a.forget} 条事实")
    m.show()


if __name__ == "__main__":
    main()
