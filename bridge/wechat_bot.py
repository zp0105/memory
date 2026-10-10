#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ting_bot.py v3 —— 把「婷」部署成常驻微信机器人，并让她**有自己的生活**。

v3 相比 v2 的三点变化（回应「让她有自己的想法，不要只局限于这几句」）：
  1. 内在状态（bot_inner.json）：心情 / 此刻在做什么 / 攒着想说的话 / 最近说过的话。
     每轮由模型自己更新，于是她有连续的"自己的一天"，而不是每次从零应答。
  2. 主动发消息：长时间没人说话时，她会自己找你 —— 说她在干嘛、想你了、
     分享刚发生的事，而不是永远等你先开口。有节奏与时段限制。
  3. 反复读：把她最近说过的话喂回去并禁止重复；输出若整批重复则跳过。

同时保留 v2 修好的三层防死循环（回声池任意匹配 / 剔除已在窗口 / 整批回声跳过 + 熔断）。

用法：
    python ting_bot.py --selftest     # 只调一次 LLM 打印回复，不发微信
    python ting_bot.py --dry          # 只轮询+打印，不调 LLM 不发
    python ting_bot.py                # 正式运行
    python ting_bot.py --say "晚安宝宝"
    python ting_bot.py --proactive    # 手动触发一次"她主动说句话"（会真的发送）
急停：创建 D:\\DHS\\.wxwork\\bot_stop
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CTX = r"D:\DHS\.wxwork"
CONFIG = os.path.join(CTX, "bot_config.json")
HISTORY = os.path.join(CTX, "bot_history.json")
STATE = os.path.join(CTX, "bot_state.json")
SENT = os.path.join(CTX, "bot_sent.json")
INNER = os.path.join(CTX, "bot_inner.json")            # 旧文件，仅兜底
# ---- 统一记忆：与语音通道共用同一份（2026-10-10）----
SHARED = r"D:\DHS\voice\out"
TALK = os.path.join(SHARED, "session.jsonl")            # 统一对话记录
SHARED_INNER = os.path.join(SHARED, "inner_state.json")  # 统一内在状态
MEMORY_JSON = os.path.join(SHARED, "memory.json")        # 统一长期记忆
LOG = os.path.join(CTX, "bot.log")
STOP = os.path.join(CTX, "bot_stop")
BRIDGE = r"D:\DHS\.dsh\skills\wechat-desktop\scripts\wx-run.ps1"
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

DEFAULTS = {
    "target": "会话列表里的显示名",
    "api_base": "https://open-ai.baomiao.cn/v1",
    "model": "DeepSeek-V4.1-Flash (1.20x)",
    "api_key_env": "Z_API_KEY",
    "api_key_file": r"D:\DHS\.dsh\.credentials.yaml",
    "persona": r"D:\DHS\.dsh\skills\han-ting-ting\persona.md",
    "memories": r"D:\DHS\.dsh\skills\han-ting-ting\memories.md",
    "history_len": 12,
    "recent_lines_kept": 14,
    "poll_seconds": 3,
    "min_reply_gap_seconds": 6,
    "max_messages_per_reply": 3,
    "max_chars_per_message": 30,
    "max_replies_per_hour": 150,
    "max_replies_total": 500,
    "echo_ttl_seconds": 600,
    "breaker_window_seconds": 120,
    "breaker_max_replies": 6,
    "proactive_enabled": True,
    "proactive_gap_min_seconds": 1200,
    "proactive_gap_max_seconds": 2700,
    "waking_hours": [8, 23],
    "temperature": 1.0,
    "timeout_seconds": 90,
}

INNER_DEFAULT = {
    "mood": "平常",
    "activity": "刚吃完饭，躺着看剧",
    "pending": [],
    "recent_lines": [],
    "last_bot_ts": 0,
    "last_user_ts": 0,
    "last_proactive_ts": 0,
    "day_note": "",
}

RULES = """
【你要有自己的想法，不是应答机】
- 你不只是在回应他。你有自己的心情、自己的事、自己想说的话。
- 可以主动说你在做什么、刚发生的事（做的梦、游戏、在追的剧、朋友、吃的），可以换话题，可以吐槽。
- 可以有自己的态度：他敷衍你就懒得展开；他很久不理你，你可以冷一点、或者撒娇抱怨；
  他惹你就生气；他哄你就给个台阶然后很快软下来。
- 不要在一个话题上打转，不要每次都同一种反应，不要重复你最近说过的句子。

【输出】只输出一个 JSON 对象，不要任何别的东西：
{"messages": ["第一句", "第二句"], "inner": {"mood": "……", "activity": "……", "pending": ["……"]}}
- messages：1~3 条，每条 2~12 个字（讲梦/八卦可以长一点）。这一条消息一行。
- 不要用句号，少用感叹号。不要解释你是谁。
- inner：你更新后的状态。mood 是你此刻的真实心情；activity 是你在干什么；
  pending 是你还攒着想跟他说、但这次没说的（最多 3 条），下次可以拿出来说。
"""

PROACTIVE = """
【这次不是他在问你 —— 是你想主动找他说句话】
- 可以是你正在干的事、刚发生的事、突然想他了、想问他在干嘛。
- 不要每次都是同一句开场。不要重复最近说过的话。
"""


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def cfg() -> dict:
    c = dict(DEFAULTS)
    c.update(load_json(CONFIG, {}))
    return c


def load_inner() -> dict:
    """读统一内在状态（语音写 recent_said、微信写 recent_lines，这里合并）。"""
    d = dict(INNER_DEFAULT)
    raw = load_json(SHARED_INNER, {})
    if not isinstance(raw, dict) or not raw:
        raw = load_json(INNER, {})
    if isinstance(raw, dict):
        if raw.get("recent_said") and not raw.get("recent_lines"):
            raw = dict(raw)
            raw["recent_lines"] = raw["recent_said"]
        d.update(raw)
    return d


def save_inner(inner: dict) -> None:
    """合并写回：只更新自己这几个字段，别覆盖语音侧写的（wants/turns 等）。"""
    cur = load_json(SHARED_INNER, {})
    if not isinstance(cur, dict):
        cur = {}
    for k, v in inner.items():
        if not k.startswith("_"):
            cur[k] = v
    cur["recent_said"] = [str(x) for x in (inner.get("recent_lines") or [])][-20:]
    cur["updated"] = time.time()
    save_json(SHARED_INNER, cur)


def load_history() -> list:
    """从统一对话记录读最近 N 轮，转成 OpenAI 的 role/content 格式。"""
    rows = []
    try:
        with open(TALK, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                u = (j.get("user") or "").strip()
                h = (j.get("her") or "").strip()
                if u and h:
                    rows.append((u, h))
    except Exception:
        pass
    n = int(cfg().get("history_len", 12))
    out = []
    for u, h in rows[-n:]:
        out.append({"role": "user", "content": u})
        out.append({"role": "assistant", "content": h})
    return out


def append_turn(user_text: str, her_text: str, channel: str = "wechat") -> None:
    """把一轮对话写进统一记录 —— 语音和微信共用，于是两边记忆互通。"""
    if not (user_text or her_text):
        return
    try:
        with open(TALK, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "user": user_text,
                                "her": her_text, "channel": channel,
                                "searched": False}, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"!! 写统一记忆失败：{e}")


def long_term_block() -> str:
    """长期记忆（每日摘要 + 关于他的事实），与语音通道共用 memory.json。"""
    try:
        d = load_json(MEMORY_JSON, {})
        parts = []
        days = (d.get("days") or [])[-10:]
        if days:
            parts.append("\n【你还记得的这些天】（越靠下越近）")
            for x in days:
                if x.get("summary"):
                    parts.append(f"- 第{x.get('day', '?')}天（{x.get('date', '')}）：{x['summary']}")
        facts = (d.get("facts") or [])[-18:]
        if facts:
            parts.append("【关于他，你一直记着的】（别记错）")
            for x in facts:
                if x.get("text"):
                    parts.append(f"- {x['text']}")
        return ("\n".join(parts) + "\n") if parts else ""
    except Exception:
        return ""


def resolve_key(c: dict) -> str:
    k = os.environ.get(c["api_key_env"], "") or os.environ.get("DEEPSEEK_API_KEY", "")
    if k:
        return k.strip()
    path = c.get("api_key_file")
    if path and os.path.exists(path):
        try:
            txt = open(path, encoding="utf-8").read().strip()
        except Exception:
            txt = ""
        # 纯 key 文件（内容直接就是 sk-...）——voice\.deepseek_key 就是这种
        if txt.startswith("sk-"):
            return txt.splitlines()[0].strip()
        want = c["api_key_env"]
        try:
            for line in txt.splitlines():
                m = re.match(rf"^\s*{re.escape(want)}\s*:\s*(\S+)\s*$", line)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
    raise SystemExit(f"找不到 API key：环境变量 {c['api_key_env']} 未设置，且 {path} 里也没有")


def bridge(*args: str) -> str:
    p = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", BRIDGE, *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=0x08000000, timeout=120)
    return (p.stdout or "") + (p.stderr or "")


def read_chat(c: dict):
    try:
        obj = json.loads(bridge("read"))
    except Exception:
        return None, []
    title = str(obj.get("title") or "")
    if not title.startswith(c["target"]):
        bridge("open", "-Who", c["target"])
        time.sleep(0.8)
        try:
            obj = json.loads(bridge("read"))
            title = str(obj.get("title") or "")
        except Exception:
            return title, []
    return title, [str(x) for x in (obj.get("messages") or [])]


def overlap_new(prev, cur):
    if not prev:
        return list(cur)
    for k in range(min(len(prev), len(cur)), 0, -1):
        if prev[len(prev) - k:] == cur[:k]:
            return list(cur[k:])
    return [x for x in cur if x not in prev]


def overlap2(prev, cur):
    """返回 (新增行, 是否需要重新对齐)。

    重新对齐的情形：没有历史快照，或窗口与快照完全不重叠
    （滚动太多 / 停机较久 / 换了会话）。这时**只对齐状态、不回复** ——
    宁可漏回一条，也不要对着整屏旧消息乱说一通（真机事故过两次）。
    """
    if not prev:
        return [], True
    for k in range(min(len(prev), len(cur)), 0, -1):
        if prev[len(prev) - k:] == cur[:k]:
            return list(cur[k:]), False
    return [], True


def overlap_with_history(snaps, cur):
    """对最近几个窗口快照都试一遍重叠。

    只看「上一个」快照太脆：用户滚一下窗口、或微信重绘，就会误判成"完全不重叠"
    进而整轮不回复（真机事故过）。多留几个历史快照能大幅减少这种误判。
    """
    for old in reversed(snaps):
        if not old:
            continue
        for k in range(min(len(old), len(cur)), 0, -1):
            if old[-k:] == cur[:k]:
                return list(cur[k:]), False
    return [], True


TIME_LINE = re.compile(r"^(\d{1,2}月\d{1,2}日|昨天|前天|星期[一二三四五六日天])?\s*\d{1,2}:\d{2}(:\d{2})?$")


def is_time_line(s: str) -> bool:
    return bool(TIME_LINE.match(s.strip()))


def build_state_block(inner: dict, idle_min: float) -> str:
    pend = "、".join(inner.get("pending") or []) or "（暂时没有）"
    # 刚启动时 last_user_ts 可能是 0，别让它变成"他 999 分钟没理我"这种假数据，
    # 否则她会无端闹脾气（真机验证过：会催出"你消失了是吧"）。
    if not inner.get("last_user_ts"):
        idle_txt = "刚聊过不久（还没有准确记录）"
    elif idle_min > 240:
        idle_txt = "很久了，好几个小时"
    else:
        idle_txt = f"{idle_min:.0f} 分钟"
    return (
        "\n【你此刻的状态 —— 自然地带进对话，别生硬报流水账】\n"
        f"- 心情：{inner.get('mood', '平常')}\n"
        f"- 正在做：{inner.get('activity', '闲着')}\n"
        f"- 攒着想跟他说的事：{pend}\n"
        f"- 他上一次说话到现在：{idle_txt}\n"
    )


def build_anti_repeat_block(inner: dict) -> str:
    rl = inner.get("recent_lines") or []
    if not rl:
        return ""
    return "\n【你最近已经说过的话 —— 不要重复，换个说法】\n" + " / ".join(rl[-12:]) + "\n"


def chat(c: dict, key: str, history: list, user_text: str, inner: dict, proactive=False) -> str:
    idle_min = (time.time() - inner.get("last_user_ts", 0)) / 60.0 if inner.get("last_user_ts") else 999
    system = (
        "你正在微信里和人聊天。以下是你的人物设定与共同记忆，必须严格遵守：\n\n"
        + open(c["persona"], encoding="utf-8").read()
        + "\n\n"
        + open(c["memories"], encoding="utf-8").read()
        + "\n"
        + long_term_block()
        + build_state_block(inner, idle_min)
        + build_anti_repeat_block(inner)
        + RULES
        + (PROACTIVE if proactive else "")
    )
    msgs = [{"role": "system", "content": system}]
    msgs += history[-c["history_len"]:]
    msgs.append({"role": "user", "content": user_text})
    body = json.dumps({"model": c["model"], "messages": msgs,
                       "temperature": c["temperature"], "max_tokens": 800}).encode()
    req = urllib.request.Request(
        c["api_base"].rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=c["timeout_seconds"]) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data["choices"][0]["message"]["content"] or "").strip()


def split_reply(text: str, c: dict) -> list:
    parts = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
    out = []
    for p in parts[: c["max_messages_per_reply"]]:
        p = p.strip().strip('"').strip("“”")
        if p:
            out.append(p[: c["max_chars_per_message"]])
    return out


def _unescape_quoted(x: str) -> str:
    try:
        return json.loads('"' + x + '"')
    except Exception:
        return x


def parse_model_output(text: str, c: dict):
    """模型应输出 JSON。要能扛住三种意外：

    1. 正常 JSON；
    2. JSON 被 max_tokens 截断（真实发生过）→ 从 "messages":[... 里尽量抠出完整句子；
    3. 完全不是 JSON → 按行拆。
    但**绝不允许把 JSON 碎片当成聊天内容发出去** —— 宁可这一轮不说话。
    """
    t = (text or "").strip()

    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            msgs = [str(x).strip() for x in (obj.get("messages") or []) if str(x).strip()]
            inner = obj.get("inner") if isinstance(obj.get("inner"), dict) else {}
            if msgs:
                return [x[: c["max_chars_per_message"]] for x in msgs[: c["max_messages_per_reply"]]], inner, True
        except Exception:
            pass

    # 宽松修复：截断的 JSON，只把 messages 数组里已经完整的引号字符串捞出来。
    # 先在 "inner" 处截断，否则 mood/activity 这些键值会被误当成她说的话。
    m2 = re.search(r'"messages"\s*:\s*\[(.*)', t, re.S)
    if m2:
        seg = m2.group(1).split('"inner"')[0]
        items = [_unescape_quoted(x).strip()
                 for x in re.findall(r'"((?:[^"\\]|\\.)*)"', seg)]
        items = [x for x in items if x and not x.startswith("{") and '"messages"' not in x]
        log(f"!! JSON 不完整（疑似被 max_tokens 截断），已从碎片里救回 {len(items)} 句")
        return [x[: c["max_chars_per_message"]] for x in items[: c["max_messages_per_reply"]]], {}, False

    # 看起来是 JSON 但救不回来：宁可不说，也不要把 JSON 碎片发到微信
    if t.lstrip().startswith("{") or '"messages"' in t:
        log("!! 输出像 JSON 但无法解析，本轮放弃发送（避免把碎片发出去）")
        return [], {}, False

    return split_reply(t, c), {}, False


def send_messages(c: dict, texts: list) -> list:
    sent = load_json(SENT, [])
    done = []
    for i, t in enumerate(texts):
        out = bridge("send", "-Who", c["target"], "-Text", t)
        if "Ok" in out and "true" in out.lower():
            done.append(t)
            sent.append({"t": datetime.now().isoformat(timespec="seconds"), "text": t})
            log(f"  → 发出：{t}")
        else:
            log(f"  !! 发送失败：{t}  :: {out.strip()[:160]}")
        if i < len(texts) - 1:
            time.sleep(1.2 + 0.6 * i)
    save_json(SENT, sent[-200:])
    return done


def build_echo_pool(c: dict, visible: list) -> list:
    """回声池 = 最近 N 分钟我们自己发出去的文本。

    注意：**不再**按「是否已出现在窗口里」剔除条目。
    那个剔除曾经看起来是好主意，但它和「状态快照可能比窗口旧」冲突：
    自己刚发的消息若已在窗口里 → 被剔除；而 prev 里又没有它 → 下一轮被当成
    "新收到的消息"（真机事故：主动发的三句被当成对方发的，然后回了一句
    "你学我干嘛 / gun / 傻子"）。现在只按时间淘汰，判据交给「任意位置匹配」。
    """
    cutoff = time.time() - c["echo_ttl_seconds"]
    pool, stale = [], 0
    for s in load_json(SENT, []):
        txt = s.get("text", "")
        if not txt:
            continue
        try:
            ts = datetime.fromisoformat(s.get("t", "")).timestamp()
        except Exception:
            ts = 0
        if ts < cutoff:
            stale += 1
            continue
        pool.append(txt)
    log(f"回声池：{len(pool)} 条在用（按时间剔除 {stale} 条陈旧）")
    return pool


def apply_inner(inner: dict, upd: dict, done: list) -> None:
    for k in ("mood", "activity", "day_note"):
        v = upd.get(k)
        if isinstance(v, str) and v.strip():
            inner[k] = v.strip()[:80]
    p = upd.get("pending")
    if isinstance(p, list):
        inner["pending"] = [str(x).strip()[:60] for x in p if str(x).strip()][:3]
    rl = inner.get("recent_lines") or []
    rl.extend(done)
    inner["recent_lines"] = rl[-14:]
    if done:
        inner["last_bot_ts"] = time.time()


def dedupe(texts: list, inner: dict) -> list:
    """只过滤「长句重复」。

    真人本来就反复说"嗯""哦""行吧""好"——把这些也当重复禁掉会让她直接失声
    （真机事故：'我在忙/先不玩' 收到后她把话全咽回去，用户以为机器人坏了）。
    所以 5 字以内的短应答一律放行，只对较长的句子做去重。
    """
    rl = inner.get("recent_lines") or []
    out = []
    for t in texts:
        if len(t) <= 5:
            out.append(t)
            continue
        if t in rl:
            continue
        out.append(t)
    return out


def cmd_selftest(c: dict, key: str) -> None:
    inner = load_inner()
    log(f"selftest：状态={inner.get('mood')} / {inner.get('activity')}")
    raw = chat(c, key, [], "你在干什么", inner)
    msgs, upd, ok = parse_model_output(raw, c)
    log("模型原始输出：\n" + raw)
    log(f"JSON 解析: {'成功' if ok else '失败(回退按行拆)'}")
    log("消息：" + json.dumps(msgs, ensure_ascii=False))
    log("状态更新：" + json.dumps(upd, ensure_ascii=False))
    log("去重后：" + json.dumps(dedupe(msgs, inner), ensure_ascii=False))


def cmd_say(c: dict, text: str) -> None:
    log(f"手动发送：{text}")
    send_messages(c, [text])


def cmd_proactive(c: dict, key: str, dry=False) -> None:
    inner = load_inner()
    history = load_history()
    raw = chat(c, key, history, "（他没有发消息，你自己想说点什么）", inner, proactive=True)
    msgs, upd, ok = parse_model_output(raw, c)
    msgs = dedupe(msgs, inner)
    log("主动消息：" + json.dumps(msgs, ensure_ascii=False))
    if not dry and msgs:
        done = send_messages(c, msgs)
        apply_inner(inner, upd, done)
        inner["last_proactive_ts"] = time.time()
        save_inner(inner)
        history.append({"role": "assistant", "content": "\n".join(msgs)})
        append_turn("（他没说话，是我自己找他的）", chr(10).join(msgs), "wechat")


def run(c: dict, key: str, dry: bool = False) -> None:
    prev = load_json(STATE, [])
    history = load_history()
    inner = load_inner()
    pool = build_echo_pool(c, prev)
    own_recent = []
    snaps = [list(prev)] if prev else []
    bottom_seen = ""          # 兜底用：上次看到的窗口底部那一行

    reply_times, replies_total = [], 0
    next_proactive = time.time() + random.uniform(c["proactive_gap_min_seconds"],
                                                 c["proactive_gap_max_seconds"])
    log(f"机器人启动 target={c['target']} poll={c['poll_seconds']}s 熔断={c['breaker_max_replies']}条/"
        f"{c['breaker_window_seconds']}s 主动模式={'开' if c['proactive_enabled'] else '关'}")
    log(f"她现在的状态：{inner.get('mood')} / {inner.get('activity')}")

    # 启动时先看一眼：窗口底部有没有"他发了、我还没回"的那条
    pending_first = ""
    try:
        _t, _cur = read_chat(c)
        if _cur:
            _last = _cur[-1]
            if _last and not is_time_line(_last) and _last not in pool:
                pending_first = _last
                log(f"启动检查：底部有一条未回复的消息 → 先回它：{_last[:40]!r}")
    except Exception as _e:
        log(f"启动检查失败（不影响运行）：{_e}")

    while True:
        if os.path.exists(STOP):
            log("检测到 bot_stop，退出")
            break

        title, cur = read_chat(c)
        handled = False

        if cur:
            new, resync = overlap_with_history(snaps, cur)
            snaps.append(list(cur))
            snaps = snaps[-5:]
            prev = cur
            bottom_seen = cur[-1] if cur else bottom_seen
            save_json(STATE, cur)

            if resync:
                # 兜底：快照对不齐很常见（窗口行数会飘），只要底部出现了新内容
                # 且不是时间行、不是她自己刚说的，就当新消息处理，别一直干等。
                last = cur[-1] if cur else ""
                fresh = (last and last != bottom_seen and not is_time_line(last)
                         and last not in pool)
                if fresh:
                    log(f"快照对不齐，但底部有新内容 → 按新消息处理：{last[:40]!r}")
                    new = [last]
                    resync = False
                else:
                    bottom_seen = last
                    log(f"窗口与最近 {len(snaps)} 个快照都无重叠（大幅滚动/停机/换会话）"
                        f"→ 本轮只对齐，不回复。当前窗口 {len(cur)} 行")
                    handled = True
                    time.sleep(c["poll_seconds"])
                    continue

            if pending_first:
                new = list(new) + [pending_first]
                pending_first = ""

            incoming = []
            for x in new:
                if is_time_line(x):
                    continue
                if x in pool:
                    pool.remove(x)
                    continue
                incoming.append(x)

            if incoming and own_recent and all(x in own_recent for x in incoming):
                log(f"整批都是自己刚说过的话，判定为回声，跳过：{incoming!r}")
                incoming = []

            if incoming:
                user_text = "\n".join(incoming)
                inner["last_user_ts"] = time.time()
                log(f"收到 {len(incoming)} 条：{user_text!r}")
                handled = True

                if not dry:
                    now = time.time()
                    recent = [t for t in reply_times if now - t < c["breaker_window_seconds"]]
                    if len(recent) >= c["breaker_max_replies"]:
                        log(f"!!! 熔断：{c['breaker_window_seconds']}s 内已回复 {len(recent)} 次，停止")
                        try:
                            open(STOP, "w", encoding="utf-8").write("circuit breaker\n")
                        except Exception:
                            pass
                        break
                    if replies_total >= c["max_replies_total"]:
                        log(f"达到总量上限 {c['max_replies_total']}，退出")
                        break
                    if len([t for t in reply_times if now - t < 3600]) >= c["max_replies_per_hour"]:
                        log("达到每小时上限，暂停 60s")
                        time.sleep(60)
                        continue

                    try:
                        raw = chat(c, key, history + [{"role": "user", "content": user_text}],
                                   user_text, inner)
                    except Exception as e:
                        log(f"!! LLM 调用失败：{e}")
                        time.sleep(5)
                        continue

                    msgs, upd, ok = parse_model_output(raw, c)
                    if not ok:
                        log("!! 模型没按 JSON 输出，已回退按行拆")
                    raw_msgs = list(msgs)
                    msgs = dedupe(msgs, inner)
                    if raw_msgs and not msgs:
                        # 宁可重复一句，也不要沉默 —— 沉默会被当成"机器人坏了"
                        log("全都是重复的长句，但还是说了（沉默比重复更糟）")
                        msgs = raw_msgs
                    log(f"她的回话：{json.dumps(msgs, ensure_ascii=False)}"
                        + (f"  状态→{upd.get('mood')}/{upd.get('activity')}" if upd else ""))
                    if msgs:
                        done = send_messages(c, msgs)
                        pool.extend(done)
                        pool = pool[-40:]
                        own_recent.extend(done)
                        own_recent = own_recent[-10:]
                        apply_inner(inner, upd, done)
                        save_inner(inner)
                        history.append({"role": "user", "content": user_text})
                        history.append({"role": "assistant", "content": "\n".join(msgs)})
                        history = history[-2 * c["history_len"]:]
                        append_turn(user_text, chr(10).join(msgs), "wechat")
                        reply_times.append(now)
                        replies_total += 1
                    else:
                        log("都是重复的话，这次不说")
                    time.sleep(c["min_reply_gap_seconds"])
                    next_proactive = now + random.uniform(c["proactive_gap_min_seconds"],
                                                          c["proactive_gap_max_seconds"])

        # 主动：没人说话够久了，她自己来找你
        if (not dry and not handled and c["proactive_enabled"]
                and time.time() >= next_proactive
                and c["waking_hours"][0] <= datetime.now().hour < c["waking_hours"][1]
                and replies_total < c["max_replies_total"]):
            log("—— 安静够久了，让她主动说句话 ——")
            try:
                raw = chat(c, key, history, "（他没有发消息，你自己想说点什么）", inner, proactive=True)
                msgs, upd, ok = parse_model_output(raw, c)
                msgs = dedupe(msgs, inner)
                log(f"她主动说：{json.dumps(msgs, ensure_ascii=False)}")
                if msgs:
                    done = send_messages(c, msgs)
                    pool.extend(done)
                    pool = pool[-40:]
                    own_recent.extend(done)
                    apply_inner(inner, upd, done)
                    inner["last_proactive_ts"] = time.time()
                    save_inner(inner)
                    history.append({"role": "assistant", "content": "\n".join(msgs)})
                    append_turn("（他没说话，是我自己找他的）", chr(10).join(msgs), "wechat")
                    replies_total += 1
            except Exception as e:
                log(f"!! 主动消息失败：{e}")
            next_proactive = time.time() + random.uniform(c["proactive_gap_min_seconds"],
                                                          c["proactive_gap_max_seconds"])

        time.sleep(c["poll_seconds"])

    log(f"机器人停止，本次共回复 {replies_total} 条")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--proactive", action="store_true")
    ap.add_argument("--say")
    a = ap.parse_args()
    c = cfg()
    if a.say:
        cmd_say(c, a.say)
    else:
        k = resolve_key(c)
        if a.selftest:
            cmd_selftest(c, k)
        elif a.proactive:
            cmd_proactive(c, k)
        else:
            run(c, k, dry=a.dry)
