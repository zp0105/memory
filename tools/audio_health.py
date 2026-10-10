"""音频体检：判断一条 wav 是「正常语音」还是「电流音 / 方波 / 削波」。

电流音（电气噪声）在波形上有很硬的指纹，不需要耳朵就能判：
  1. 削波率：样本贴在满量程（|x| >= 0.98）的比例。方波音频会 >30%，正常语音 <1%
  2. 谱平坦度（SFM）：几何均值/算术均值。白噪声≈1，语音≈0.0x
  3. 高频能量占比：4kHz 以上能量占比。电流音/嘶嘶声很高
  4. 过零率：方波/噪声远高于语音
用法：run.cmd audio_health.py [a.wav b.wav ...]
"""
import os
import sys
import wave

import numpy as np


def read(p):
    with wave.open(p, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise SystemExit(f"{p}: 不支持 {sw*8}bit")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return sr, x


def analyze(p):
    sr, x = read(p)
    n = len(x)
    dur = n / sr
    peak = float(np.max(np.abs(x))) if n else 0.0
    rms = float(np.sqrt(np.mean(x ** 2))) if n else 0.0
    clip_frac = float(np.mean(np.abs(x) >= 0.98)) if n else 0.0
    near = float(np.mean(np.abs(x) >= 0.9)) if n else 0.0
    # 削波还会有大量样本落在同一个极值上
    top = float(np.max(np.bincount((np.abs(x) * 32767).astype(np.int32), minlength=1)) / max(n, 1))
    zcr = float(np.mean(np.abs(np.diff(np.signbit(x).astype(np.int8))))) if n > 1 else 0.0
    # 谱特征（只取有声帧，避免静音把结论拉偏）
    win = 1024
    hop = 512
    if n >= win:
        idx = np.arange(win)[None, :] + hop * np.arange(max(1, (n - win) // hop))[:, None]
        fr = x[idx] * np.hanning(win)[None, :]
        rms_f = np.sqrt(np.mean(fr ** 2, axis=1) + 1e-12)
        keep = fr[rms_f > max(np.percentile(rms_f, 60), 1e-4)]
        if len(keep) == 0:
            keep = fr
        P = np.abs(np.fft.rfft(keep, axis=1)) ** 2 + 1e-12
        P = P.mean(axis=0)
        freqs = np.fft.rfftfreq(win, 1.0 / sr)
        sfm = float(np.exp(np.mean(np.log(P))) / np.mean(P))
        hf = float(P[freqs >= 4000].sum() / P.sum())
        band_lo = P[(freqs >= 100) & (freqs < 1000)].sum()
        band_hi = P[freqs >= 4000].sum()
        ratio = float(band_hi / max(band_lo, 1e-12))
    else:
        sfm = hf = ratio = float("nan")
    verdict = "正常语音"
    reasons = []
    if clip_frac > 0.05 or top > 0.25:
        verdict = "**电流音/削波（方波）**"
        reasons.append(f"削波率 {clip_frac*100:.1f}%、最常见样本值占 {top*100:.0f}%")
    if sfm > 0.25:
        verdict = "**噪声**" if verdict == "正常语音" else verdict
        reasons.append(f"谱平坦度 {sfm:.2f}（语音通常 <0.1）")
    if hf > 0.5:
        reasons.append(f"4k 以上能量 {hf*100:.0f}%")
    print(f"\n=== {os.path.basename(p)} ===")
    print(f"  采样率 {sr}  时长 {dur:.2f}s  峰值 {peak:.3f}  RMS {rms:.3f}")
    print(f"  削波率 {clip_frac*100:5.2f}%   最常见样本值占比 {top*100:5.1f}%   过零率 {zcr:.3f}")
    print(f"  谱平坦度 {sfm:.3f}   高频(>4k)占比 {hf*100:5.1f}%   4k/1k 能量比 {ratio:.3f}")
    print(f"  结论：{verdict}" + ("  ← " + "；".join(reasons) if reasons else ""))
    return dict(path=p, sr=sr, dur=dur, peak=peak, rms=rms, clip=clip_frac,
                top=top, zcr=zcr, sfm=sfm, hf=hf, verdict=verdict)


def main():
    files = sys.argv[1:]
    if not files:
        files = sorted(__import__("glob").glob(r"D:\DHS\voice\out\gsv\*.wav"))
        files += [r"D:\DHS\voice\out\last_turn.wav"]
    bad = 0
    for f in files:
        if not os.path.exists(f):
            print(f"缺失 {f}")
            continue
        r = analyze(f)
        if r["verdict"] != "正常语音":
            bad += 1
    print(f"\n体检 {len(files)} 条，可疑 {bad} 条")


if __name__ == "__main__":
    main()
