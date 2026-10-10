"""她的内在状态：心情、正在做的事、还惦记的事、刚说过的话。

为什么需要它：之前的回复是「在干嘛」→「看剧呢/男主又围上来一个女生」这种固定映射，
问 6 次答 6 次一样 —— 那不是人，是背台词。
这里让状态随每次对话推进（由 LLM 基于她的性格演化），并且记下她刚说过的话，
下一轮明确禁止重复，同时给她随机的「日常素材」和「好奇心种子」。
"""
import json
import os
import random
import re
import time

STATE_PATH = r"D:\DHS\voice\out\inner_state.json"

# 她的日常素材池（来自 persona/记忆里的真实生活纹理，随机取样避免每次都说同一件）
DAILY_POOL = [
    "刚下课回宿舍，书包一扔就躺下了",
    "在食堂吃了碗面，有点咸",
    "跟弟弟拌了两句嘴，他把我袜子拿去套嘴上了",
    "在宿舍洗衣服，水房排队排好久",
    "妈妈让我帮她买个东西，我还没去",
    "网课开着没在听，在偷偷刷手机",
    "在追剧，男主身边又围上来一个女生",
    "室友在放歌，吵得我头疼",
    "刚睡醒，头发乱得不行",
    "在算这个月生活费还剩多少",
    "弟弟喊我晚上打游戏",
    "在外面走着呢，今天风挺大",
    "在考虑要不要去剪个头发",
    "刚吃了个火鸡面，辣死我了",
    "作业还没写，一点不想动",
]

# 她的好奇心种子（用来触发"自己去查"）
CURIOSITY_SEEDS = [
    "最近有什么好听的歌",
    "最近有什么好看的剧",
    "BLACKPINK 最近有什么动静",
    "最近有什么新鲜事",
    "大学里最近有什么热搜",
]


def _norm(s):
    """粗归一：去标点空白，便于比较重复。"""
    return re.sub(r"[\s，。！？、,.!?~…'\"（）()「」]+", "", s or "")


def _topics_of(said, n=3):
    """把她说过的几句话压成"话题标签"，而不是原文——给原文会被复读。"""
    keys = []
    for s in said[-n:]:
        t = _norm(s)
        # 取前 6 个字当话题提示（不是完整句子，不足以复读）
        if t:
            keys.append(t[:6])
    return " / ".join(reversed(keys))


class InnerState:
    def __init__(self, path=STATE_PATH):
        self.path = path
        self.data = {
            "mood": "还行，懒懒的",
            "activity": "躺着追剧",
            "pending": [],
            "wants": [],
            "recent_said": [],      # 她最近说过的话（防重复）
            "recent_heard": [],     # 你最近说的话
            "topic_seed": "",
            "turns": 0,
            "updated": 0.0,
        }
        self.load()
        # 从对话日志补齐"她说过的话"——否则每次重启都会重新说同样的话题
        self._hydrate_from_log()

    def _hydrate_from_log(self, n=8):
        try:
            log = r"D:\DHS\voice\out\session.jsonl"
            if not os.path.exists(log):
                return
            lines = open(log, encoding="utf-8").read().strip().split("\n")[-n:]
            said, heard = [], []
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    j = json.loads(ln)
                except Exception:
                    continue
                if j.get("her"):
                    said.append(str(j["her"]))
                if j.get("user"):
                    heard.append(str(j["user"]))
            if said:
                old = self.data.get("recent_said") or []
                self.data["recent_said"] = (old + said)[-14:]
            if heard:
                old = self.data.get("recent_heard") or []
                self.data["recent_heard"] = (old + heard)[-8:]
        except Exception:
            pass

    def load(self):
        if os.path.exists(self.path):
            try:
                d = json.load(open(self.path, encoding="utf-8"))
                self.data.update(d)
            except Exception:
                pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.data["updated"] = time.time()
            json.dump(self.data, open(self.path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ---------- 防重复 ----------
    def remember_said(self, text):
        t = (text or "").strip()
        if not t:
            return
        rs = self.data.setdefault("recent_said", [])
        rs.append(t)
        self.data["recent_said"] = rs[-12:]
        self.data["turns"] = int(self.data.get("turns", 0)) + 1

    def remember_heard(self, text):
        t = (text or "").strip()
        if not t:
            return
        rh = self.data.setdefault("recent_heard", [])
        rh.append(t)
        self.data["recent_heard"] = rh[-8:]

    def is_repeat(self, candidate):
        """与最近说过的话高度重合则视为重复。

        短句（如「看剧呢」）更容易被漏掉，所以对短句用更严的字符重合度。
        """
        c = _norm(candidate)
        # 太短的片段（"我…""嗯"）不算重复，否则会把正常开头误杀
        if len(c) < 3:
            return False
        for old in self.data.get("recent_said", [])[-8:]:
            o = _norm(old)
            if not o or len(o) < 2:
                continue
            if c == o or c in o or o in c:
                return True
            sc, so = set(c), set(o)
            inter = len(sc & so)
            if not inter:
                continue
            ratio = inter / max(len(sc), 1)          # 本条被覆盖的比例
            rev = inter / max(len(so), 1)            # 旧话被覆盖的比例
            if len(c) <= 5:
                # 短句：几乎完全一样才算重复，避免误杀正常短答
                if ratio >= 0.99 and rev >= 0.8:
                    return True
            elif ratio >= 0.75 and rev >= 0.6:
                return True
        return False

    # ---------- 注入 prompt ----------
    def pick_seed(self):
        seed = random.choice(CURIOSITY_SEEDS)
        self.data["topic_seed"] = seed
        return seed

    def prompt_block(self, with_seed=False):
        d = self.data
        said = d.get("recent_said", [])[-5:]
        heard = d.get("recent_heard", [])[-4:]
        daily = random.sample(DAILY_POOL, 2)
        lines = [
            "\n\n【你此刻的真实状态】",
            f"- 心情：{d.get('mood', '')}",
            f"- 正在做：{d.get('activity', '')}",
        ]
        if d.get("pending"):
            lines.append("- 心里还惦记的事：" + "；".join(d["pending"][:3]))
        pending = d.get("wants") or []
        lines.append("- 今天你身边的零碎（可以挑一件说，但别每次都提同一件）：" +
                     "；".join(daily + pending[:2]))
        if heard:
            lines.append("- 他刚跟你说过的（别重复问他、也别复读他的话）：" + " / ".join(heard))
        if said:
            # 【关键】这里**不能**列出她说过的话的原文——列出来模型会直接复读，
            # 等于给它递了稿子。只说"已经聊过的轮数"，逼它自己找新内容。
            topics = _topics_of(said)
            lines.append(
                f"- 你已经连着说了 {len(said)} 轮。刚才聊过的话题大概有：{topics}。\n"
                "  **这一轮必须换新的东西说**：今天别的琐事、你刚看到/吃到/想到的、"
                "身上真实的感受。不要重复上面那些话题的说法，也不要复述他刚说的话。"
            )
        if with_seed:
            lines.append(f"- 你心里正琢磨着想去搜一件事：{d.get('topic_seed') or self.pick_seed()}")
        return "\n".join(lines) + "\n"

    def similar_to_recent(self, text, jac_thresh=0.5, cover_thresh=0.68):
        """整段相似度查重：长回复也要拦（包含关系查不出"换个说法复读"）。"""
        def grams(s, n=3):
            s = _norm(s)
            if len(s) < n:
                return {s} if s else set()
            return {s[i:i + n] for i in range(len(s) - n + 1)}

        g = grams(text)
        if not g:
            return False
        for old in self.data.get("recent_said", [])[-8:]:
            o = grams(old)
            if not o:
                continue
            inter = len(g & o)
            if inter == 0:
                continue
            jac = inter / max(len(g | o), 1)
            cover = inter / max(len(g), 1)
            if jac > jac_thresh or cover > cover_thresh:
                return True
        return False


def build_update_prompt(persona_md, user_text, her_text, prev):
    """让 LLM 以她的视角推进内心状态（保持性格，不要大变）。"""
    sys_p = (
        "你在维护一个真实人物（__NAME__）的内心状态，用来让她在连续对话里保持一致性、"
        "又不会每次说一样的话。\n"
        "下面是她的性格设定：\n" + persona_md[:3000] + "\n\n"
        "请根据刚刚这一轮对话，输出她此刻更新的内心状态。要求：\n"
        "1. mood/activity/pending/wants 都要跟着上面的对话自然演化，允许变化，但不要突然变成另一个人。\n"
        "2. activity 必须是具体的一件小事（比如「在宿舍洗衣服」「躺着刷手机」），"
        "不要笼统写「待着」「休息」。\n"
        "3. pending 最多 3 条：她心里想对他说、但还没说出口的话。\n"
        "4. wants 最多 3 条：她今天想做的具体小事（想吃什么、想去哪、想干嘛），"
        "必须跟前面的不重复。\n"
        "5. mood 要具体（「有点小得意」「懒懒的不想动」），不要写「正常」「一般」。\n"
        "只输出 JSON，不要解释。格式：\n"
        '{"mood":"...","activity":"...","pending":["..."],"wants":["..."]}'
    )
    user_p = (
        f"上一轮状态：{json.dumps({k: prev.get(k) for k in ('mood', 'activity', 'pending', 'wants')}, ensure_ascii=False)}\n"
        f"他刚才说：{user_text}\n"
        f"她回的是：{her_text}\n"
        "请给出这一轮之后她的内心状态。"
    )
    return [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}]


def apply_update(state, raw_text):
    """把 LLM 返回的 JSON 解析并写回（容错）。"""
    if not raw_text:
        return False
    m = re.search(r"\{.*\}", raw_text, re.S)
    if not m:
        return False
    try:
        j = json.loads(m.group(0))
    except Exception:
        return False
    changed = False
    for k in ("mood", "activity"):
        v = j.get(k)
        if isinstance(v, str) and v.strip():
            state.data[k] = v.strip()[:60]
            changed = True
    for k in ("pending", "wants"):
        v = j.get(k)
        if isinstance(v, list):
            vals = [str(x).strip()[:40] for x in v if str(x).strip()][:3]
            state.data[k] = vals
            changed = True
    if changed:
        state.save()
    return changed


if __name__ == "__main__":
    st = InnerState()
    print("当前状态:", json.dumps({k: v for k, v in st.data.items()
                                   if k not in ("recent_said", "recent_heard")},
                                  ensure_ascii=False))
    print("\n注入块示例:\n" + st.prompt_block(with_seed=True))
    print("重复检测:", st.is_repeat("躺着追剧呢那个男主又围上来一个女生"))
