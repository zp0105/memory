"""回声判断：用「她正在播的声音」当参考，判断麦克风里听到的是不是她自己。

背景：她的声音从耳机/扬声器漏回麦克风，VAD 会判成"你在说话"，
导致她刚开口就被自己打断（日志 `⚡ 打断（VAD 0.99）`）。

思路（比逐样本 NLMS 稳得多）：
- 播放时记录"播放到的时间点"和对应的参考音频（1 秒环形缓存）；
- 麦克风每来一块，取同时刻附近的参考片段，在 0~700ms 延迟里找最佳相关；
- 相关性高 → 这是她自己的声音（回灌），不算打断；
- 相关性低但 VAD 高 → 真的有人在说话，可以打断。

用法：
  python echosub.py            # 现场自检：播她的声音，报告相关性判定是否可用
"""
import collections
import sys
import time

import numpy as np

SR_MIC = 16000
SR_REF = 24000


def resample16k(x, sr_from):
    if sr_from == SR_MIC or len(x) == 0:
        return np.asarray(x, dtype=np.float32)
    n = int(round(len(x) * SR_MIC / sr_from))
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


class EchoJudge:
    """维护参考缓存并给出「麦克风 vs 她的播放」的相关系数。"""

    def __init__(self, cache_sec=1.2, max_delay_ms=700, min_lag_ms=10, step_ms=5):
        self.cache_len = int(cache_sec * SR_MIC)
        self.ref = np.zeros(self.cache_len, dtype=np.float32)
        self.filled = 0
        self.max_lag = int(max_delay_ms * SR_MIC / 1000)
        self.min_lag = int(min_lag_ms * SR_MIC / 1000)
        self.step = max(1, int(step_ms * SR_MIC / 1000))
        self.last = {"corr": 0.0, "lag_ms": 0.0}

    def push_ref(self, audio, sr=SR_REF):
        """追加她正在播的音频（自动重采样到 16k），保留最近 cache_len 个样本。"""
        x = resample16k(audio, sr)
        if len(x) == 0:
            return
        if len(x) >= self.cache_len:
            self.ref = x[-self.cache_len:].astype(np.float32).copy()
            self.filled = self.cache_len
            return
        self.ref = np.concatenate([self.ref[len(x):], x]).astype(np.float32)
        self.filled = min(self.cache_len, self.filled + len(x))

    def corr(self, mic):
        """返回 (最大相关系数, 对应延迟ms)。粗搜 + 精搜两级对齐。"""
        if self.filled < SR_MIC // 8 or len(mic) < 64:
            return 0.0, 0.0
        mic = np.asarray(mic, dtype=np.float32)
        seg_len = min(len(mic), int(0.15 * SR_MIC))       # 用 150ms 做相关
        seg = mic[-seg_len:]
        seg = seg - seg.mean()
        seg_norm = np.linalg.norm(seg)
        if seg_norm < 1e-6:
            return 0.0, 0.0

        def score(lag):
            end = self.cache_len - lag
            start = end - seg_len
            if start < 0 or end > self.cache_len:
                return -1.0
            rs = self.ref[start:end]
            if len(rs) < 64:
                return -1.0
            rs = rs - rs.mean()
            rn = np.linalg.norm(rs)
            if rn < 1e-6:
                return -1.0
            return float(np.dot(seg, rs) / (seg_norm * rn))

        # 一级：粗搜（步长 10ms）
        best_c, best_lag = 0.0, -1
        for lag in range(self.min_lag, self.max_lag, self.step):
            c = score(lag)
            if c > best_c:
                best_c, best_lag = c, lag
        if best_lag < 0:
            return 0.0, 0.0
        # 二级：在最优附近 ±12ms 细搜（步长 1ms），对齐精度决定相关性上限
        fine = max(1, SR_MIC // 1000)
        lo = max(self.min_lag, best_lag - 12 * fine)
        hi = min(self.max_lag, best_lag + 12 * fine)
        for lag in range(lo, hi + 1):
            c = score(lag)
            if c > best_c:
                best_c, best_lag = c, lag
        self.last = {"corr": best_c, "lag_ms": best_lag * 1000.0 / SR_MIC}
        return best_c, best_lag * 1000.0 / SR_MIC

    def is_self_echo(self, mic, thresh=0.25):
        c, _ = self.corr(mic)
        return c >= thresh, c


class EchoGate:
    """打断门控：攒够一段麦克风音频再判，压住相关性搜索的 CPU 开销。"""

    def __init__(self, buf_ms=120, thresh=0.25, step_ms=10):
        self.judge = EchoJudge(step_ms=step_ms)
        self.buf_ms = buf_ms
        self.buf_len = int(buf_ms * SR_MIC / 1000)
        self.thresh = thresh
        self.buf = []
        self.n = 0

    def push_ref(self, audio, sr=SR_REF):
        self.judge.push_ref(audio, sr)

    def feed(self, mic_chunk):
        """喂入麦克风样本；攒够了就返回 (是否回灌, 相关系数)，否则返回 None。"""
        self.buf.append(mic_chunk)
        self.n += len(mic_chunk)
        if self.n < self.buf_len:
            return None
        x = np.concatenate(self.buf)
        self.buf = [x[-self.buf_len // 2:]]      # 保留一半，避免边界丢信息
        self.n = len(self.buf[0])
        return self.judge.is_self_echo(x, self.thresh)


def feedback_probe(seconds=2.0, out_dev=None, in_dev=None):
    """现场自检：用她自己的克隆语音播放，同时录音，看相关性能不能区分出来。"""
    import pyaudio
    from vad import SileroVad, WIN

    sys.path.insert(0, r"D:\DHS\voice")
    ref_wav = r"D:\DHS\voice\out\tts_A.wav"
    import wave
    with wave.open(ref_wav, "rb") as w:
        sr = w.getframerate()
        speech = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        speech = np.tile(speech, 2)[:int(sr * seconds)]

    pa = pyaudio.PyAudio()
    inp = pa.open(format=pyaudio.paInt16, channels=1, rate=SR_MIC, input=True,
                  frames_per_buffer=WIN, input_device_index=in_dev)
    out = pa.open(format=pyaudio.paInt16, channels=1, rate=sr, output=True,
                  frames_per_buffer=512, output_device_index=out_dev)
    judge = EchoJudge()
    vad = SileroVad()
    data = (np.clip(speech, -1, 1) * 32767).astype("<i2").tobytes()
    step = 512 * 2                     # 每次播 512 帧（约 21ms @24k）
    mic = []
    probs, corrs = [], []
    print(f"播放她的语音 {len(speech)/sr:.1f}s 并录音…")
    for i in range(0, len(data), step):
        chunk = data[i:i + step]
        out.write(chunk)
        judge.push_ref(speech[i // 2:(i + step) // 2], sr)
        if inp.get_read_available() >= WIN:
            raw = inp.read(WIN, exception_on_overflow=False)
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            mic.append(x)
            probs.append(vad.speech_prob(x))
            c, lag = judge.corr(x)
            corrs.append(c)
    out.stop_stream(); out.close()
    inp.stop_stream(); inp.close()
    pa.terminate()

    m = np.concatenate(mic) if mic else np.zeros(WIN, dtype=np.float32)
    rms = float(np.sqrt(np.mean(m ** 2) + 1e-12))
    pr = np.array(probs) if probs else np.zeros(1)
    cr = np.array(corrs) if corrs else np.zeros(1)
    print(f"麦克风: RMS {20*np.log10(rms+1e-12):.1f}dBFS  VAD 最大 {pr.max():.2f} 均值 {pr.mean():.2f}")
    print(f"相关性: 最大 {cr.max():.2f} 均值 {cr.mean():.2f}  最后延迟 {judge.last['lag_ms']:.0f}ms")
    hi = cr > 0.25
    if hi.any():
        print(f"=> 相关性可判定：{hi.mean()*100:.0f}% 的帧被识别为「她自己的声音」")
        print("   这些帧不该触发打断。")
    else:
        print("=> 相关性偏低：回灌可能很弱，或延迟超出搜索范围。")


if __name__ == "__main__":
    feedback_probe()
