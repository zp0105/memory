"""联网搜索：给她一个"自己查"的工具（免 key，走 bing.cn / so.com / sogou 兜底）。

用法：
  python websearch.py 北京今天天气
"""
import html
import re
import sys
import time
from urllib.parse import quote, unquote

import requests

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def clean(s):
    s = TAG.sub("", s or "")
    s = html.unescape(s)
    return WS.sub(" ", s).strip()


def _get(url, timeout=15):
    r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    # 让 requests 按响应头猜编码，中文站点常见 gb2312/utf-8
    if not r.encoding or r.encoding.lower() in ("iso-8859-1",):
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def search_bing(query, limit=5):
    url = f"https://cn.bing.com/search?q={quote(query)}&ensearch=0"
    body = _get(url)
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', body, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = clean(m.group(2))
        link = html.unescape(m.group(1))
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = clean(sm.group(1)) if sm else ""
        if title:
            out.append({"title": title, "url": link, "snippet": snippet, "src": "bing"})
        if len(out) >= limit:
            break
    return out


def search_so360(query, limit=5):
    url = f"https://www.so.com/s?q={quote(query)}"
    body = _get(url)
    out = []
    for block in re.findall(r'<li class="res-list.*?</li>', body, re.S):
        m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = clean(m.group(2))
        link = html.unescape(m.group(1))
        sm = re.search(r'<p[^>]*class="res-desc"[^>]*>(.*?)</p>', block, re.S) or \
             re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = clean(sm.group(1)) if sm else ""
        if title:
            out.append({"title": title, "url": link, "snippet": snippet, "src": "360"})
        if len(out) >= limit:
            break
    return out


def search_sogou(query, limit=5):
    url = f"https://www.sogou.com/web?query={quote(query)}"
    body = _get(url)
    out = []
    for block in re.findall(r'<div class="vrwrap[^"]*".*?</div>\s*</div>', body, re.S):
        m = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = clean(m.group(2))
        link = html.unescape(m.group(1))
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = clean(sm.group(1)) if sm else ""
        if title:
            out.append({"title": title, "url": link, "snippet": snippet, "src": "sogou"})
        if len(out) >= limit:
            break
    return out


ENGINES = [search_bing, search_so360, search_sogou]

# 宽泛的"要闻"类查询：普通网页搜索会返回百科/歌曲这类垃圾，改走实时热搜
NEWSY = ("新闻", "新鲜事", "热点", "热搜", "最近发生", "今天发生", "头条", "大事")


def is_newsy(query):
    return any(k in query for k in NEWSY)


def normalize_query(query):
    """把太宽泛的说法收紧一点，避免搜出字面匹配（如「最近」搜到李圣杰的歌）。"""
    q = (query or "").strip()
    q = re.sub(r"[\s\u3000]+", " ", q)
    q = re.sub(r"^(帮我)?(搜一下|查一下|搜索|查查|看看)", "", q).strip()
    # 「最近/最新/近期 + 有什么/热播/推荐...」这类问法会按字面匹配，先剥掉套话
    q = re.sub(r"(最近|最新|近期)(有什么|有啥|哪些|有没有|有什么好的|有什么好看的)?", "", q).strip()
    q = re.sub(r"^(有什么|有啥|哪些|有没有|有推荐的?)", "", q).strip()
    q = re.sub(r"(热播|热门的?)", "", q).strip()
    q = re.sub(r"\s+", "", q).strip()      # 空格会让 bing 跑偏，去掉
    if not q:
        return query.strip()
    # 内容类查询补"推荐"，结果质量会明显变好（且不要带空格）
    if re.search(r"(剧|电视剧|综艺|电影|动漫|番|歌|音乐|专辑)", q) and "推荐" not in q:
        q = re.sub(r"[？?。！!]", "", q) + "推荐"
    return q.strip() or query.strip()


def hot_results(limit=6):
    """走百度实时热搜，返回与 search() 同结构的结果。"""
    try:
        from hotlist import hot_list
    except Exception:
        return []
    hs = hot_list(limit)
    return [{"title": t, "url": "", "snippet": s, "src": "hot"} for t, s in hs]


def search(query, limit=5, tries=3):
    """依次尝试：热点榜（问要闻时）→ 各搜索引擎。"""
    q = normalize_query(query)
    if is_newsy(q):
        hs = hot_results(limit)
        if hs:
            return hs
    for fn in ENGINES[:tries]:
        try:
            t = time.time()
            res = fn(q, limit=limit)
            if res:
                for r in res:
                    r["elapsed"] = round(time.time() - t, 2)
                return res
        except Exception:
            continue
    return []


def format_for_llm(results, query, max_snippet=120):
    if not results:
        return f"（搜「{query}」没搜到东西）"
    if all(r.get("src") == "hot" for r in results):
        lines = ["（这是此刻的热搜榜）"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
        return "\n".join(lines)
    lines = []
    for i, r in enumerate(results, 1):
        sn = (r["snippet"] or "")[:max_snippet]
        lines.append(f"{i}. {r['title']}：{sn}")
    return "\n".join(lines)


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "今天有什么新闻"
    t = time.time()
    rs = search(q)
    print(f"查询「{q}」用时 {time.time()-t:.2f}s 得到 {len(rs)} 条")
    for r in rs:
        print(f"  [{r['src']}] {r['title']}")
        print(f"      {r['snippet'][:100]}")
        print(f"      {r['url'][:90]}")
