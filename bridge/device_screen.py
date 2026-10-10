"""设备屏模拟：一个竖屏小窗口，显示她的立绘、对话和在一起的天数。

这就是将来桌面设备屏幕上要显示的东西——先用窗口模拟，硬件到货后同样的设计搬过去。
用法：run.cmd device_screen.py [--w 420 --h 760]
"""
import argparse
import json
import os
import sys
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont

from PIL import Image, ImageTk

sys.path.insert(0, r"D:\DHS\voice")

PORTRAIT = r"D:\DHS\voice\out\portrait\official_main.png"
SESSION = r"D:\DHS\voice\out\session.jsonl"
STATE = r"D:\DHS\voice\out\diary_state.json"
BG = "#0f1115"
FG = "#e8e8ea"
DIM = "#8a8f98"
ACCENT = "#c8484f"


class Screen:
    def __init__(self, w=420, h=760):
        self.root = tk.Tk()
        self.root.title("她 —— 设备屏")
        # 别超出屏幕：高度最多到屏幕的 88%
        sh = self.root.winfo_screenheight()
        h = min(h, int(sh * 0.88))
        self.w, self.h = w, h
        x = max(20, self.root.winfo_screenwidth() - w - 40)
        self.root.geometry(f"{w}x{h}+{x}+30")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        f_big = tkfont.Font(family="Microsoft YaHei", size=15, weight="bold")
        f_mid = tkfont.Font(family="Microsoft YaHei", size=11)
        f_small = tkfont.Font(family="Microsoft YaHei", size=9)

        # 顶部：天数 + 状态
        top = tk.Frame(self.root, bg=BG, height=50)
        top.pack(fill="x", padx=16, pady=(10, 2))
        self.lb_days = tk.Label(top, text="", bg=BG, fg=FG, font=f_big, anchor="w")
        self.lb_days.pack(side="left")
        self.lb_status = tk.Label(top, text="● 待机", bg=BG, fg=DIM, font=f_small, anchor="e")
        self.lb_status.pack(side="right")

        # 立绘：先按宽度适配，再限高（保证完整显示，不裁人）
        self.img_label = tk.Label(self.root, bg=BG)
        self.img_label.pack(pady=(2, 6))
        self._load_portrait(max_h=h - 330)

        tk.Frame(self.root, bg="#22262e", height=1).pack(fill="x", padx=16)

        # 对话区
        self.talk = tk.Frame(self.root, bg=BG)
        self.talk.pack(fill="both", expand=True, padx=16, pady=8)
        self.rows = []
        for _ in range(4):
            r = tk.Label(self.talk, text="", bg=BG, fg=DIM, font=f_mid,
                         wraplength=w - 60, justify="left", anchor="w")
            r.pack(fill="x", pady=2)
            self.rows.append(r)

        self.lb_hint = tk.Label(self.root, text="", bg=BG, fg="#4a4f57", font=f_small)
        self.lb_hint.pack(pady=(0, 8))

        self.refresh()
        self.root.after(2000, self.tick)

    def _load_portrait(self, max_h):
        try:
            im = Image.open(PORTRAIT).convert("RGB")
            avail_w = self.w - 48
            # 1) 按宽度放大/缩小
            ratio = avail_w / im.width
            im = im.resize((avail_w, max(1, int(im.height * ratio))), Image.LANCZOS)
            # 2) 太高就按高度缩，然后居中裁宽（只裁两侧背景，不裁人）
            if im.height > max_h > 0:
                r2 = max_h / im.height
                nw = max(1, int(im.width * r2))
                im = im.resize((nw, max_h), Image.LANCZOS)
                if nw > avail_w:
                    left = (nw - avail_w) // 2
                    im = im.crop((left, 0, left + avail_w, max_h))
            self.photo = ImageTk.PhotoImage(im)
            self.img_label.configure(image=self.photo, text="")
        except Exception as e:
            self.img_label.configure(text=f"（立绘加载失败：{e}）", fg=DIM)

    def days(self):
        try:
            st = json.load(open(STATE, encoding="utf-8"))
            s = st.get("start_date")
            if s:
                d0 = datetime.strptime(s, "%Y-%m-%d").date()
                return (datetime.now().date() - d0).days + 1
        except Exception:
            pass
        return 1

    def recent(self, n=4):
        out = []
        if os.path.exists(SESSION):
            for line in open(SESSION, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                out.append((j.get("user", ""), j.get("her", "")))
        return out[-n:]

    def refresh(self):
        self.lb_days.configure(text=f"第 {self.days()} 天")
        rows = self.recent()
        for i, lb in enumerate(self.rows):
            if i < len(rows):
                u, h = rows[i]
                txt = f"你：{u}\n她：{h}"
                lb.configure(text=txt, fg=FG if i == len(rows) - 1 else "#6f747c")
            else:
                lb.configure(text="")
        mtime = os.path.getmtime(SESSION) if os.path.exists(SESSION) else 0
        idle = (datetime.now().timestamp() - mtime) if mtime else 9999
        if idle < 15:
            self.lb_status.configure(text="● 说话中", fg=ACCENT)
        elif idle < 120:
            self.lb_status.configure(text="● 刚聊完", fg="#7aa2f7")
        else:
            self.lb_status.configure(text="● 待机", fg=DIM)
        self.lb_hint.configure(text="设备屏模拟 · 真机到货后搬过去")

    def tick(self):
        try:
            self.refresh()
        except Exception:
            pass
        self.root.after(2000, self.tick)

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=420)
    ap.add_argument("--h", type=int, default=760)
    a = ap.parse_args()
    Screen(a.w, a.h).run()


if __name__ == "__main__":
    main()
