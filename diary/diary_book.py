"""把她的日记汇总成一本可以翻阅的网页（日记本.html）。

用法：
  run.cmd diary_book.py          # 生成
  run.cmd diary_book.py --open   # 生成并打开
"""
import argparse
import os
import re
import sys
from datetime import datetime

DIARY = r"D:\DHS\voice\out\diary"
OUT = os.path.join(DIARY, "日记本.html")
PORTRAIT = r"D:\DHS\voice\out\portrait\official_main.png"

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日记本</title>
<style>
  :root {{ --paper:#f6f1e7; --ink:#3a3630; --dim:#8c8378; --red:#c8484f; }}
  * {{ box-sizing:border-box; }}
  html,body {{ max-width:100%; overflow-x:hidden; }}
  body {{ margin:0; background:#e8e2d6; color:var(--ink);
         font-family:"Kaiti SC","KaiTi","STKaiti","Microsoft YaHei",serif; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:28px 18px 80px; }}
  header {{ text-align:center; margin-bottom:26px; }}
  header h1 {{ font-size:30px; margin:0 0 6px; letter-spacing:2px; }}
  header .sub {{ color:var(--dim); font-size:14px; }}
  .cover {{ display:flex; align-items:center; gap:16px; background:var(--paper);
            border-radius:14px; padding:14px; margin:18px 0 26px;
            box-shadow:0 2px 10px rgba(0,0,0,.08); }}
  .cover img {{ width:96px; height:auto; border-radius:10px; }}
  .cover .who {{ font-size:15px; line-height:1.7; }}
  .cover .who b {{ font-size:17px; }}
  .entry {{ overflow:hidden; background:var(--paper); border-radius:14px; padding:22px 24px;
            margin:0 0 22px; box-shadow:0 3px 14px rgba(0,0,0,.09);
            position:relative; }}
  .entry .day {{ font-size:13px; color:var(--dim); letter-spacing:1px; }}
  .entry .date {{ float:right; font-size:13px; color:var(--dim); }}
  .entry .text {{ margin:14px 0 0; font-size:17.5px; line-height:2.05; }}
  .entry .text p {{ margin:0 0 10px; word-break:break-word;
                    overflow-wrap:anywhere; }}
  .entry .text p:last-child {{ margin-bottom:0; }}
  .entry img {{ display:block; margin:18px auto 4px; max-width:100%;
                border-radius:10px; background:#fff; }}
  .entry .cap {{ text-align:center; color:var(--dim); font-size:13px; }}
  .empty {{ text-align:center; color:var(--dim); padding:60px 0; }}
  footer {{ text-align:center; color:var(--dim); font-size:13px; margin-top:34px; }}
</style></head><body><div class="wrap">
<header>
  <h1>她 的 日 记 本</h1>
  <div class="sub">{made} 更新 · 一共 {n} 篇</div>
</header>
{cover}
{entries}
<footer>这些是她自己写的，每天 22:00 自动添一篇。</footer>
</div></body></html>
"""


def read_entries():
    """返回 [(date, day, text, img_name)]，新的在前。"""
    items = []
    if not os.path.isdir(DIARY):
        return items
    for name in sorted(os.listdir(DIARY), reverse=True):
        if not name.endswith(".md"):
            continue
        date_str = name[:-3]
        p = os.path.join(DIARY, name)
        try:
            body = open(p, encoding="utf-8").read()
        except Exception:
            continue
        m = re.search(r"第\s*(\d+)\s*天", body)
        day = m.group(1) if m else "?"
        # 去掉标题行/图片行，剩下的当正文
        lines = []
        img = None
        for l in body.splitlines():
            s = l.strip()
            if not s:
                continue
            if s.startswith("#") or s.startswith("第 ") and "天" in s and len(s) < 14:
                continue
            mi = re.search(r"!\[[^\]]*\]\(([^)]+)\)", s)
            if mi:
                img = mi.group(1)
                continue
            lines.append(s)
        text = "\n".join(lines)
        items.append((date_str, day, text, img))
    return items


def build():
    items = read_entries()
    if items:
        cov = ""
        if os.path.exists(PORTRAIT):
            cov = ('<div class="cover">'
                   f'<img src="../portrait/{os.path.basename(PORTRAIT)}" alt="她">'
                   '<div class="who"><b>__NAME__</b><br>'
                   '这些话我没跟他说过，都写在这儿了。<br>'
                   '他不许偷看——不过他要真看了，我也不生气。</div></div>')
        blocks = []
        for date_str, day, text, img in items:
            img_html = ""
            if img:
                img_html = (f'<img src="{img}" alt="她画的">'
                            f'<div class="cap">她画的</div>')
            paras = "".join(f"<p>{l}</p>" for l in text.split(chr(10)) if l.strip())
            blocks.append(
                f'<div class="entry"><span class="day">第 {day} 天</span>'
                f'<span class="date">{date_str}</span>'
                f'<div class="text">{paras}</div>{img_html}</div>')
        entries = "\n".join(blocks)
    else:
        cov = ""
        entries = '<div class="empty">她还没写过日记。<br>跑一次 `run.cmd diary.py` 就有了。</div>'
    html = HTML.format(made=datetime.now().strftime("%Y-%m-%d %H:%M"),
                       n=len(items), cover=cov, entries=entries)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"日记本: {OUT}（{len(items)} 篇）")
    return OUT


def _open_in_browser(path):
    """直接用浏览器打开（系统没配 .html 关联时 os.startfile 会弹选择框）。"""
    import subprocess
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for exe in cands:
        if os.path.exists(exe):
            subprocess.Popen([exe, path])
            print(f"用 {os.path.basename(exe)} 打开")
            return True
    try:
        import webbrowser
        webbrowser.open("file:///" + path.replace("\\", "/"))
        return True
    except Exception as e:
        print(f"打不开，请手动打开：{path}（{e}）")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    p = build()
    if a.open:
        _open_in_browser(p)


if __name__ == "__main__":
    main()
