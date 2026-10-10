"""推送到手机（不依赖微信客户端在线）。

支持三条通道（配一个就能用，配多个会都发）：
  1. Server酱  sct.ftqq.com         —— 微信扫码绑定拿 SendKey，最省事
  2. PushPlus  pushplus.plus        —— 同类，免费额度更多
  3. 邮件 SMTP                       —— 用你的邮箱发给自己

配置：D:\\DHS\\voice\\notify.json
{
  "serverchan_key": "SCTxxxxxxxx",
  "pushplus_token": "",
  "email": {"smtp": "smtp.qq.com", "port": 465, "user": "you@qq.com",
            "pass": "授权码", "to": "you@qq.com"}
}

用法：
  run.cmd notify.py --test                 # 三个通道各发一条测试
  run.cmd notify.py --title "标题" --text "正文"
"""
import argparse
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText

CONF = r"D:\DHS\voice\notify.json"


def load_conf():
    try:
        return json.load(open(CONF, encoding="utf-8"))
    except Exception:
        return {}


def send_serverchan(key, title, text):
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": title, "desp": text}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    ok = j.get("code") == 0
    return ok, json.dumps(j, ensure_ascii=False)[:160]


def send_pushplus(token, title, text):
    url = "http://www.pushplus.plus/send"
    body = json.dumps({"token": token, "title": title, "content": text,
                       "template": "html"}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    ok = j.get("code") == 200
    return ok, json.dumps(j, ensure_ascii=False)[:160]


def send_email(cfg, title, text, attachments=None):
    """发邮件给自己。attachments 可以带图（比如她画的简笔画）。"""
    import ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders

    host = cfg.get("smtp") or "smtp.qq.com"
    port = int(cfg.get("port") or 465)
    user = cfg.get("user") or ""
    pwd = cfg.get("pass") or ""
    to = cfg.get("to") or user
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(text, "plain", "utf-8"))
        for path in attachments:
            try:
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as f:
                    part = MIMEBase("image", "png")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment",
                                filename=("utf-8", "", os.path.basename(path)))
                msg.attach(part)
            except Exception as e:
                print(f"  （附件 {os.path.basename(path)} 添加失败：{e}）")
    else:
        msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    n = len([p for p in (attachments or []) if os.path.exists(p)])
    return True, f"邮件已发到 {to}" + (f"（带 {n} 个附件）" if n else "")


def send(title, text, only=None, attachments=None):
    c = load_conf()
    results = []
    if (only in (None, "sc")) and c.get("serverchan_key"):
        try:
            results.append(("Server酱",) + send_serverchan(c["serverchan_key"], title, text))
        except Exception as e:
            results.append(("Server酱", False, f"{type(e).__name__}: {str(e)[:100]}"))
    if (only in (None, "pp")) and c.get("pushplus_token"):
        try:
            results.append(("PushPlus",) + send_pushplus(c["pushplus_token"], title, text))
        except Exception as e:
            results.append(("PushPlus", False, f"{type(e).__name__}: {str(e)[:100]}"))
    if (only in (None, "mail")) and (c.get("email") or {}).get("user"):
        try:
            results.append(("邮件",) + send_email(c["email"], title, text, attachments))
        except Exception as e:
            results.append(("邮件", False, f"{type(e).__name__}: {str(e)[:100]}"))
    if not results:
        results.append(("（未配置任何通道）", False,
                        f"请先编辑 {CONF}，填 serverchan_key / pushplus_token / email 任一"))
    for name, ok, info in results:
        print(f"  {'✓' if ok else '✗'} {name}: {info}")
    return any(r[1] for r in results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="来自__NAME__")
    ap.add_argument("--text", default="这是一条测试消息。")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--only", default=None, help="sc / pp / mail")
    a = ap.parse_args()
    if a.test:
        print("正在测试推送通道 ...")
        send("【测试】她想跟你说句话", "如果你在手机上看到这条，说明通道通了。\n\n"
             "以后她写完日记就会这样推给你。", only=a.only)
    else:
        send(a.title, a.text, only=a.only)


if __name__ == "__main__":
    main()
