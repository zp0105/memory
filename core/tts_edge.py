"""Edge TTS 引擎：接口与本地 CosyVoice 的 Tts 兼容。

好处：不占本地显卡（微软云端合成），所以 A 卡 / 没独显的电脑也能让她说话。
代价：音色是预置的，不是她本人的声音（想要本人音色需要云端声音复刻）。

  from edge_tts_engine import EdgeTts
  t = EdgeTts("zh-CN-XiaoxiaoNeural")
  for chunk in t.stream("你咋才回我呀"):   # chunk: float32 numpy, 24kHz
      ...
"""
import asyncio
import os
import subprocess
import sys

import numpy as np

SR = 24000

# 中文音色（中文名 -> voice id）
VOICES = {
    "晓晓": "zh-CN-XiaoxiaoNeural",          # 标准女声·温暖（默认）
    "晓伊": "zh-CN-XiaoyiNeural",            # 标准女声·活泼
    "小北": "zh-CN-liaoning-XiaobeiNeural",  # 辽宁话·北方口音
    "小妮": "zh-CN-shaanxi-XiaoniNeural",    # 陕西话
    "云希": "zh-CN-YunxiNeural",             # 男声
}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def _ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class EdgeTts:
    def __init__(self, voice=DEFAULT_VOICE, sr=SR, rate="+0%", pitch="+0Hz"):
        self.voice = VOICES.get(voice, voice)
        self.sr = sr
        self.rate = rate        # 例如 "+10%" 说得快一点
        self.pitch = pitch      # 例如 "-10Hz" 音调低一点

    # ---- 内部：合成 mp3（异步） ----
    def _synth_mp3(self, text):
        import edge_tts

        async def go():
            c = edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch)
            buf = bytearray()
            async for ch in c.stream():
                if ch.get("type") == "audio":
                    buf.extend(ch["data"])
            return bytes(buf)

        return asyncio.run(go())

    # ---- 内部：mp3 -> pcm float32 ----
    def _decode(self, mp3_bytes):
        cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error",
               "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le",
               "-ar", str(self.sr), "-ac", "1", "pipe:1"]
        p = subprocess.run(cmd, input=mp3_bytes, capture_output=True)
        if p.returncode != 0 or not p.stdout:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(p.stdout, dtype="<i2").astype(np.float32) / 32768.0

    # ---- 对外：与本地 Tts 一致的流式接口 ----
    def stream(self, text, emotion=None, chunk_sec=0.1):
        text = (text or "").strip()
        if not text:
            return
        try:
            mp3 = self._synth_mp3(text)
        except Exception as e:
            print(f"  [EdgeTTS] 合成失败：{type(e).__name__}: {str(e)[:100]}", flush=True)
            return
        pcm = self._decode(mp3)
        step = max(1, int(self.sr * chunk_sec))
        for i in range(0, len(pcm), step):
            yield pcm[i:i + step]

    def warm_others_async(self):
        """本地引擎用来预热情绪参考；云端不需要，留空兼容。"""
        return

    def synth_all(self, text, emotion=None):
        return np.concatenate(list(self.stream(text, emotion))) if text.strip() else \
            np.zeros(0, dtype=np.float32)


if __name__ == "__main__":
    import wave
    sys.path.insert(0, r"D:\DHS\voice")
    text = sys.argv[1] if len(sys.argv) > 1 else "你咋才回我呀，我都等半天了。"
    voice = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VOICE
    t = EdgeTts(voice)
    import time
    t0 = time.time()
    audio = t.synth_all(text)
    el = time.time() - t0
    print(f"音色 {t.voice}  音频 {len(audio)/t.sr:.2f}s  合成 {el:.2f}s  "
          f"RTF {el/max(len(audio)/t.sr,1e-6):.2f}")
    out = r"D:\DHS\voice\out\edge_tts\engine_test.wav"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(t.sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    print("样本:", out)
