"""导出多情绪参考音频（给"按情绪换腔调"用）。

从 her_voices.wav 按映射表切出，转 16k 单声道，写入 ref16k/，并更新 manifest。
"""
import json
import os
import wave

import numpy as np

SRC = r"D:\DHS\.wxwork\silk\her_voices.wav"
MAP = r"D:\DHS\.wxwork\silk\her_voices_map.json"
SV = r"D:\DHS\.wxwork\silk\her_voice_texts_sv.json"
OUT = r"D:\DHS\voice\ref16k"

# 情绪 -> (编号, 用途说明)。
# 【重要教训】用户两次反馈"开心和委屈都太极端"、"不像人"：
#   - 242（起伏 9.1 半音）合成后音节率只有 1.82，像机器拖长音；
#   - 声学差异拉得越大，参考音频的韵律越变形，合成越不像真人。
# 结论：情绪腔调以"温和但有差别"为准，宁小不大。
#   307 开心：语速正常(3.1)、起伏 2.59，听着自然
#   196 委屈：f0 238、起伏 3.42，温和（不用 298 那种低平慢的极端）
#   210 日常：最稳的一条，作为默认
EMOTION_REFS = {
    "开心": (307, "因为能见到俺亲爱的同学了，我太激动了。"),
    "委屈": (196, "我都想录个视频让你看看，但是一想算了吧，你可能会被恶心到。"),
    "生气": (306, "你滚吧，张鹏，我都不想搭理你了。"),
    "无奈": (330, "我下凉被忘子了，我又跑回来拿，我真服了。"),
    "慵懒": (279, "好吧，我还想多躺会儿呢，但是俺二姑说一会来找俺啊，好烦。"),
    "日常": (210, "没事，反正也不是我唱的。"),
}


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def resample(x, a, b):
    n = int(round(len(x) * b / a))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def save(path, x, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def main():
    x, sr = read_wav(SRC)
    items = {it["i"]: it for it in json.load(open(MAP, encoding="utf-8"))["items"]}
    sv = json.load(open(SV, encoding="utf-8"))
    texts = {}
    if isinstance(sv, list):
        for k, v in enumerate(sv):
            texts[k] = v if isinstance(v, str) else (v.get("text") or "")
    elif isinstance(sv, dict):
        texts = {int(k): (v if isinstance(v, str) else v.get("text", "")) for k, v in sv.items()}

    manifest = {}
    mpath = os.path.join(OUT, "manifest.json")
    if os.path.exists(mpath):
        for m in json.load(open(mpath, encoding="utf-8")):
            manifest[m["i"]] = m

    os.makedirs(OUT, exist_ok=True)
    # 先清掉所有旧的情绪标记，避免同一编号既被标"委屈"又被标"开心"
    for m in manifest.values():
        m.pop("emotion", None)
    for emo, (idx, ref_text) in EMOTION_REFS.items():
        it = items.get(idx)
        if not it:
            print(f"跳过 {emo}：映射里没有 {idx}")
            continue
        s = int(it["start_ms"] * sr / 1000)
        e = min(int(it["end_ms"] * sr / 1000), len(x))
        seg = resample(x[s:e], sr, 16000)
        pad = np.zeros(int(0.05 * 16000), dtype=np.float32)
        seg = np.concatenate([pad, seg, pad])
        p = os.path.join(OUT, f"ref_{idx:03d}_16k.wav")
        save(p, seg, 16000)
        t = (texts.get(idx) or ref_text).strip()
        manifest[idx] = {"i": idx, "wav": p, "dur": round(len(seg) / 16000, 2), "text": t,
                         "emotion": emo}
        print(f"  {emo:4} #{idx:3} {len(seg)/16000:4.2f}s  {t[:34]}")

    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(sorted(manifest.values(), key=lambda m: m["i"]), f,
                  ensure_ascii=False, indent=1)
    print(f"\nmanifest 已更新：{mpath}（共 {len(manifest)} 条）")
    # 输出情绪映射，供 gateway 使用
    print("\n情绪映射：")
    for emo, (idx, _) in EMOTION_REFS.items():
        print(f'    "{emo}": {idx},')


if __name__ == "__main__":
    main()
