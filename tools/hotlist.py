"""热搜榜抓取（百度实时热搜），用于「最近有什么新闻」这类宽泛问题。"""
import html
import json
import re
import time

import requests

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
URL = "https://top.baidu.com/board?tab=realtime"
TAG = re.compile(r"<[^>]+>")


def _clean(s):
    return re.sub(r"\s+", " ", html.unescape(TAG.sub("", s or ""))).strip()


def hot_list(limit=8):
    """返回 [(标题, 摘要/热度), ...]。"""
    try:
        r = requests.get(URL, headers=UA, timeout=15)
        r.raise_for_status()
        body = r.text
    except Exception:
        return []
    out = []

    # 方式一：页面内嵌 JSON（最稳）
    for m in re.finditer(r'"word"\s*:\s*"([^"]{2,40})"', body):
        w = _clean(m.group(1))
        if w and w not in [x[0] for x in out]:
            out.append((w, ""))
        if len(out) >= limit:
            return out

    # 方式二：HTML 结构
    if not out:
        for block in re.findall(r'class="c-single-text-ellipsis"[^>]*>(.*?)</div>', body, re.S):
            t = _clean(block)
            if t and len(t) > 1 and t not in [x[0] for x in out]:
                out.append((t, ""))
            if len(out) >= limit:
                break
    return out[:limit]


def format_hot(limit=6):
    hs = hot_list(limit)
    if not hs:
        return ""
    lines = [f"{i}. {t}" for i, (t, _) in enumerate(hs, 1)]
    return "\n".join(lines)


if __name__ == "__main__":
    t = time.time()
    hs = hot_list(10)
    print(f"拿到 {len(hs)} 条热搜，用时 {time.time()-t:.2f}s")
    for i, (w, s) in enumerate(hs, 1):
        print(f"  {i}. {w}")
