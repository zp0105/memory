"""她的日记本 + 手绘简笔画（参考用户给的第二个视频做的原型）。

三件事：
1. 记天数（你来了第 N 天）
2. 每天写一篇日记（用她自己的口吻，结合当天的对话和她的内在状态）
3. 亲手画一幅简笔画（"我们两个"），线条是程序画的，每次略有不同

用法：
  run.cmd diary.py                # 生成今天的
  run.cmd diary.py --days 3       # 补生成最近 3 天
  run.cmd diary.py --show         # 生成后打开图片
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, r"D:\DHS\voice")

OUT = r"D:\DHS\voice\out\diary"
STATE = r"D:\DHS\voice\out\diary_state.json"
SESSION = r"D:\DHS\voice\out\session.jsonl"
CONFIG = r"D:\DHS\.wxwork\bot_config.json"
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def days_with_you():
    st = load_json(STATE, {})
    start = st.get("start_date")
    if not start:
        start = datetime.now().strftime("%Y-%m-%d")
        st["start_date"] = start
        json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    return (datetime.now().date() - d0).days + 1


def today_talk(n=14):
    """取当天（或最近）的对话，作为日记素材。"""
    if not os.path.exists(SESSION):
        return []
    rows = []
    for line in open(SESSION, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        ts = j.get("t") or 0
        if datetime.fromtimestamp(ts).date() == datetime.now().date():
            rows.append((j.get("user", ""), j.get("her", "")))
    return rows[-n:]


def gen_diary_text(days, talk, state):
    """用她的口吻写今天的日记。"""
    from gateway import Llm
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    llm = Llm(cfg)
    talk_txt = "\n".join(f"  他：{u}\n  我：{h}" for u, h in talk) or "（今天没怎么说话）"
    st = state or {}
    sys_p = (
        "你在替__NAME__写她自己的日记。日记是写给她自己看的，不是写给他的，所以更松、更软一点。\n"
        "她的性格：话少、嘴硬心软、用骂人表达亲昵、北方口语（俺/咱/搁/咋/可）。\n"
        "要求：\n"
        "1. 不要用「亲爱的」「宝贝」这种甜腻称呼开场，她是冷淡的写法。\n"
        "2. 先说今天的具体小事（用对话里的细节），不要空泛抒情。\n"
        "3. 中间会插一句「画了什么」，因为下面要配一张她画的画。\n"
        "4. 结尾要有一句没说完的、藏着的话（比如「等你回来…」「算了，不写了」）。\n"
        "5. 150~220 字，分成 3~5 段短句。不要用 emoji、不要用 Markdown 标题。\n"
        "6. 只输出日记正文，不要写日期、不要写「日记」两个字。"
    )
    user_p = (
        f"今天是第 {days} 天。\n"
        f"我今天的心情：{st.get('mood', '还行')}\n"
        f"我正在做：{st.get('activity', '躺着')}\n"
        f"今天跟他的对话：\n{talk_txt}\n"
        "另外：我今天给他画了一张画，画的是我们两个——两个小人，"
        "左边是我，右边是他，中间画了一颗心。日记里要提到这张画（别写得太详细，一两句）。\n"
        "请写今天的日记。"
    )
    try:
        return llm.complete([{"role": "system", "content": sys_p},
                             {"role": "user", "content": user_p}],
                            temperature=1.0, timeout=90).strip()
    except Exception as e:
        return f"（写日记失败：{type(e).__name__} {str(e)[:60]}）"


def draw_us(path, days, her_name="__NAME__", his_name="你"):
    """画"我们两个"：两个简笔小人 + 爱心 + 名字。每次手抖一点，像真的手画。"""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 900, 620
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    font = None
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                font = ImageFont.truetype(f, 30)
                font_small = ImageFont.truetype(f, 24)
                font_title = ImageFont.truetype(f, 40)
                break
            except Exception:
                continue
    if font is None:
        font = font_small = font_title = ImageFont.load_default()

    def wobble(x, y, r=3):
        return x + random.randint(-r, r), y + random.randint(-r, r)

    def person(cx, cy, scale=1.0, smile=True, hair="long"):
        """火柴人：头 + 身体 + 手臂 + 腿。手绘感靠轻微抖动。"""
        r = int(46 * scale)
        # 头
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="black", width=3)
        # 头发（她是长发）
        if hair == "long":
            d.arc([cx - r - 6, cy - r - 10, cx + r + 6, cy + r + 26], 200, 340,
                  fill="black", width=4)
            d.line([cx - r + 4, cy - 6, cx - r - 2, cy + r + 30], fill="black", width=3)
            d.line([cx + r - 4, cy - 6, cx + r + 2, cy + r + 30], fill="black", width=3)
        # 眼睛
        ex = int(16 * scale)
        ey = int(-6 * scale)
        d.ellipse([cx - ex - 4, cy + ey - 4, cx - ex + 4, cy + ey + 4], fill="black")
        d.ellipse([cx + ex - 4, cy + ey - 4, cx + ex + 4, cy + ey + 4], fill="black")
        # 嘴
        mw = int(14 * scale)
        if smile:
            d.arc([cx - mw, cy + 2, cx + mw, cy + 2 + mw], 0, 180, fill="black", width=3)
        else:
            d.line([cx - mw, cy + 14, cx + mw, cy + 14], fill="black", width=3)
        # 身体
        by0 = cy + r
        by1 = by0 + int(86 * scale)
        d.line([cx, by0, cx, by1], fill="black", width=3)
        # 手臂
        ay = by0 + int(26 * scale)
        d.line([cx, ay, cx - int(46 * scale), ay + int(26 * scale)], fill="black", width=3)
        d.line([cx, ay, cx + int(46 * scale), ay + int(26 * scale)], fill="black", width=3)
        # 腿
        d.line([cx, by1, cx - int(34 * scale), by1 + int(56 * scale)], fill="black", width=3)
        d.line([cx, by1, cx + int(34 * scale), by1 + int(56 * scale)], fill="black", width=3)
        return by1 + int(56 * scale)

    # 左边：她（高一点、有头发）；右边：你（图上那个"呆"的）
    lx, ly = 300, 210
    rx, ry = 600, 230
    random.seed(int(time.time()) % 100000)
    person(lx, ly, 1.0, smile=True, hair="long")
    person(rx, ry, 0.95, smile=False, hair="short")

    # 中间的爱心（手画的两个弧）
    hx, hy = (lx + rx) // 2, 190
    d.arc([hx - 34, hy - 26, hx, hy + 10], 180, 360, fill="black", width=3)
    d.arc([hx, hy - 26, hx + 34, hy + 10], 180, 360, fill="black", width=3)
    d.line([hx - 33, hy - 2, hx, hy + 40], fill="black", width=3)
    d.line([hx + 33, hy - 2, hx, hy + 40], fill="black", width=3)

    # 名字（放在腿下方，别被线条压住）
    d.text((lx - 40, ly + 195), her_name, font=font, fill="black")
    d.text((rx - 30, ry + 195), his_name, font=font_small, fill="black")
    # 落款
    d.text((60, 520), f"第 {days} 天，我画的。", font=font_small, fill=(90, 90, 90))
    d.text((60, 560), "你不许说丑，我已经尽力了。", font=font_small, fill=(90, 90, 90))

    img.save(path)
    return path


def write_md(days, text, img_path, date_str):
    p = os.path.join(OUT, f"{date_str}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"# {date_str}\n\n")
        f.write(f"第 {days} 天\n\n")
        f.write(text + "\n\n")
        f.write(f"![我们两个]({os.path.basename(img_path)})\n")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--force", action="store_true", help="今天已写过也重写")
    ap.add_argument("--sd", action="store_true", help="额外用 SD 生成一张场景图")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    md_path = os.path.join(OUT, f"{date_str}.md")
    if os.path.exists(md_path) and not a.force:
        print(f"今天（{date_str}）的日记已经写过了：{md_path}")
        print("要重写就加 --force")
        return
    from inner_state import InnerState
    st = InnerState()
    days = days_with_you()
    talk = today_talk()
    print(f"第 {days} 天，今天有 {len(talk)} 轮对话素材")
    text = gen_diary_text(days, talk, st.data)
    img = draw_us(os.path.join(OUT, f"{date_str}.png"), days)
    md = write_md(days, text, img, date_str)
    print("\n--- 日记 ---")
    print(text)
    print(f"\n日记: {md}\n配图: {img}")
    # 可选：再生成一张 SD 场景图（更精美，成本约 7 秒）
    if a.sd:
        try:
            import subprocess
            print("生成场景图 ...", flush=True)
            subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "portrait.py"),
                            "--scene", "diary", "--n", "1"], check=False)
        except Exception as e:
            print(f"场景图失败：{type(e).__name__} {str(e)[:60]}")
    # 记录进 diary_state
    try:
        s = load_json(STATE, {})
        s.setdefault("entries", []).append({"date": date_str, "days": days,
                                            "text": text, "img": os.path.basename(img)})
        json.dump(s, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    # 推送到手机（不依赖微信在线；没配通道就静默跳过）
    try:
        from notify import send as _notify
        _body = "第 %d 天（%s）\n\n%s\n\n---\n她还画了一张画。" % (days, date_str, text)
        _notify("日记 · 第%d天" % days, _body, attachments=[img])
    except Exception as _e:
        print(f"（推送跳过：{type(_e).__name__}）")

    # 日记写完顺手刷新日记本网页
    try:
        import subprocess
        subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(__file__), diary_book.py)],
                       check=False)
    except Exception:
        pass
    if a.show:
        os.startfile(img)


if __name__ == "__main__":
    main()
