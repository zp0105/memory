"""长音频转写：按静音切段，逐段跑 SenseVoice，输出带时间戳的全文。

用法：run.cmd long_transcribe.py <wav> [每段秒数]
"""
import os
import sys
import wave

import numpy as np
import sherpa_onnx

MODEL_DIR = r"D:\DHS\.wxwork\sensevoice"
SR = 16000


def read(p):
    with wave.open(p, "rb") as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1)
    if sr != SR:
        n = int(len(x) * SR / sr)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


def main():
    path = sys.argv[1]
    seg_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
    x = read(path)
    print(f"音频 {len(x)/SR:.1f}s")
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(MODEL_DIR, "model.int8.onnx"),
        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
        num_threads=4, use_itn=True, language="zh", debug=False)

    seg = int(seg_sec * SR)
    out = []
    for i in range(0, len(x), seg):
        chunk = x[i:i + seg]
        if len(chunk) < SR * 0.4:
            continue
        # 太安静就跳过
        if float(np.sqrt(np.mean(chunk ** 2) + 1e-12)) < 0.004:
            continue
        s = rec.create_stream()
        s.accept_waveform(SR, chunk)
        rec.decode_stream(s)
        t = s.result.text.strip()
        if t:
            mm, ss = divmod(int(i / SR), 60)
            out.append((f"{mm:02d}:{ss:02d}", t))
    print()
    for ts, t in out:
        print(f"[{ts}] {t}")
    full = "".join(t for _, t in out)
    print(f"\n=== 全文（{len(full)} 字）===")
    print(full)
    with open(r"D:\DHS\voice\out\video\transcript.txt", "w", encoding="utf-8") as f:
        for ts, t in out:
            f.write(f"[{ts}] {t}\n")
        f.write("\n=== 全文 ===\n" + full + "\n")
    print("\n已存 out/video/transcript.txt")


if __name__ == "__main__":
    main()
