"""手机访问服务：让手机在同一个 WiFi 下看她的日记和状态。

起一个 HTTP 服务（默认 8780），根目录就是 out/，所以日记本、立绘、简笔画都能看。
另外提供一个手机首页（/）和二维码（/qr.png），省得在手机上敲地址。

用法：
  run.cmd phone_server.py              # 起服务并打印地址+二维码
  run.cmd phone_server.py --port 9000
手机浏览器打开：http://<电脑IP>:8780/
"""
import argparse
import io
import json
import os
import socket
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

OUT = r"D:\DHS\voice\out"
DIARY = os.path.join(OUT, "diary")
STATE = os.path.join(OUT, "diary_state.json")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def days_with_you():
    try:
        st = json.load(open(STATE, encoding="utf-8"))
        d0 = datetime.strptime(st["start_date"], "%Y-%m-%d").date()
        return (datetime.now().date() - d0).days + 1
    except Exception:
        return 1


def latest_diary():
    """返回最近一篇日记的 (日期, 正文行, 图片名)。"""
    try:
        names = sorted([n for n in os.listdir(DIARY) if n.endswith(".md")], reverse=True)
    except Exception:
        return None
    if not names:
        return None
    p = os.path.join(DIARY, names[0])
    try:
        body = open(p, encoding="utf-8").read()
    except Exception:
        return None
    text, img = [], None
    for l in body.splitlines():
        s = l.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("第 ") and "天" in s and len(s) < 14:
            continue
        if s.startswith("!["):
            i = s.find("(") + 1
            img = s[i:s.find(")")] if i else None
            continue
        text.append(s)
    return names[0][:-3], text, img


HOME = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>__NAME__</title>
<style>
  :root {{ --paper:#f6f1e7; --ink:#3a3630; --dim:#8c8378; --red:#c8484f; }}
  * {{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }}
  html,body {{ max-width:100%; overflow-x:hidden; }}
  body {{ margin:0; background:#e8e2d6; color:var(--ink);
         font-family:"Kaiti SC","KaiTi","Microsoft YaHei",serif; }}
  .wrap {{ max-width:560px; margin:0 auto; padding:18px 14px 60px; }}
  .hero {{ text-align:center; }}
  .hero img {{ width:66%; max-width:240px; border-radius:16px;
               box-shadow:0 6px 22px rgba(0,0,0,.18); }}
  .hero h1 {{ font-size:26px; margin:14px 0 2px; letter-spacing:3px; }}
  .hero .day {{ color:var(--red); font-size:15px; letter-spacing:1px; }}
  .card {{ background:var(--paper); border-radius:14px; padding:18px 16px;
           margin:16px 0; box-shadow:0 3px 14px rgba(0,0,0,.09);
           overflow:hidden; }}
  .card h2 {{ font-size:16px; margin:0 0 10px; color:var(--dim);
              font-weight:normal; letter-spacing:1px; }}
  .card .text {{ font-size:16.5px; line-height:2; }}
  .card .text p {{ margin:0 0 10px; word-break:break-word; overflow-wrap:anywhere; }}
  .card .text p:last-child {{ margin-bottom:0; }}
  .card img {{ display:block; width:100%; border-radius:10px; margin-top:12px;
               background:#fff; }}
  a.btn {{ display:block; text-align:center; background:var(--red); color:#fff;
           text-decoration:none; padding:15px; border-radius:12px;
           font-size:17px; letter-spacing:2px; margin:18px 0;
           box-shadow:0 4px 14px rgba(200,72,79,.3); }}
  footer {{ text-align:center; color:var(--dim); font-size:12.5px; margin-top:26px; }}
</style></head><body><div class="wrap">
<div class="hero">
  <img src="/portrait/official_main.png" alt="__NAME__">
  <h1>__NAME__</h1>
  <div class="day">第 {days} 天</div>
</div>
<a class="btn" href="/diary/日记本.html">翻她的日记本 →</a>
{latest}
<footer>{now} · 手机和电脑要在同一个 WiFi 下</footer>
</div></body></html>
"""


def home_html():
    d = latest_diary()
    if d:
        date_str, text, img = d
        img_html = f'<img src="/diary/{img}" alt="她画的">' if img else ""
        paras = "".join(f"<p>{t}</p>" for t in text)
        latest = (f'<div class="card"><h2>最新一篇 · {date_str}</h2>'
                  f'<div class="text">{paras}</div>{img_html}</div>')
    else:
        latest = '<div class="card">她还没写过日记。</div>'
    return HOME.format(days=days_with_you(),
                       latest=latest,
                       now=datetime.now().strftime("%Y-%m-%d %H:%M"))


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=OUT, **kw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = home_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/qr.png"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(QR_PNG)
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


QR_PNG = b""


def make_qr(url):
    global QR_PNG
    try:
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        QR_PNG = buf.getvalue()
        return img
    except Exception as e:
        print(f"  二维码生成失败：{e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    ip = lan_ip()
    url = f"http://{ip}:{a.port}/"
    print(f"\n  手机上打开： {url}")
    print(f"  （本机也能看：  http://127.0.0.1:{a.port}/）")
    img = make_qr(url)
    if img is not None:
        try:
            img.save(os.path.join(OUT, "phone_qr.png"))
            print(f"  二维码图片： {os.path.join(OUT, 'phone_qr.png')}（扫它更快）")
        except Exception:
            pass
    print("\n  Ctrl+C 停止\n")
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), H)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止")


if __name__ == "__main__":
    main()
