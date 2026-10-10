"""GPT-SoVITS 引擎：不用 NVIDIA 显卡也能跑出「她本人的声音」。

为什么用它替代 CosyVoice2：VITS 架构比 0.5B 的 CosyVoice2 轻得多。
实测纯 CPU：CosyVoice2 RTF 7.7 vs GPT-SoVITS RTF 1.33（快 6 倍）。

接口与本地 Tts / EdgeTts 一致：stream(text, emotion) 产出 float32 chunk。
"""
import os
import sys
import time

import numpy as np

GSV = r"D:\DHS\voice\GPT-SoVITS-src"
REF_FALLBACK = (r"D:\DHS\voice\ref16k\ref_210_16k.wav", "没事，反正也不是我唱的。")


class GsvTts:
    def __init__(self, ref_wav=None, ref_text=None, device="cpu", version="v2"):
        # 注意：(None, None) 是"非空元组"，用 or 兜不住，得逐个兜
        self.ref_wav = ref_wav or REF_FALLBACK[0]
        self.ref_text = ref_text or REF_FALLBACK[1]
        self.device = device
        # GPT-SoVITS 内部有相对路径假设，必须切到它的根目录
        if GSV not in sys.path:
            sys.path.insert(0, GSV)
        sys.path.insert(0, os.path.join(GSV, "GPT_SoVITS"))
        self._cwd = os.getcwd()
        os.chdir(GSV)
        from GPT_SoVITS.TTS_infer_pack.TTS import TTS
        cfg = {"custom": {
            "device": device, "is_half": (device != "cpu"), "version": version,
            "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
            "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
            "cnnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
            "t2s_weights_path": "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/"
                                "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
            "vits_weights_path": "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth",
        }}
        t0 = time.time()
        self.tts = TTS(cfg)
        self.sr = 32000
        # 预热一次：首块有个固定的初始化开销（实测从 4.5s 降到 1.4s）
        try:
            self._run("嗯。")
            self._run("好。")
        except Exception as e:
            print(f"  [GSV] 预热失败（不影响使用）：{type(e).__name__}", flush=True)
        os.chdir(self._cwd)
        print(f"[TTS] GPT-SoVITS（device={device}）就绪 {time.time()-t0:.1f}s",
              flush=True)

    def _run(self, text, speed=1.0):
        inputs = {
            "text": text, "text_lang": "zh",
            "ref_audio_path": self.ref_wav, "prompt_text": self.ref_text,
            "prompt_lang": "zh",
            "top_k": 15, "top_p": 1, "temperature": 1,
            "text_split_method": "cut0", "batch_size": 1, "speed_factor": speed,
            "fragment_interval": 0.05, "seed": 42, "parallel_infer": True,
            "repetition_penalty": 1.35,
        }
        chunks = []
        for sr_i, a in self.tts.run(inputs):
            self.sr = sr_i
            a = np.asarray(a)
            a = a.astype(np.float32) / 32768.0 if np.issubdtype(a.dtype, np.integer) \
                else a.reshape(-1).astype(np.float32)
            chunks.append(a)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def stream(self, text, emotion=None, chunk_sec=0.1):
        text = (text or "").strip()
        if not text:
            return
        try:
            cwd = os.getcwd()
            os.chdir(GSV)
            audio = self._run(text)
            os.chdir(cwd)
        except Exception as e:
            print(f"  [GSV] 合成失败：{type(e).__name__}: {str(e)[:100]}", flush=True)
            return
        step = max(1, int(self.sr * chunk_sec))
        for i in range(0, len(audio), step):
            yield audio[i:i + step]

    def warm_others_async(self):
        return

    def synth_all(self, text, emotion=None):
        return np.concatenate(list(self.stream(text, emotion))) if text.strip() \
            else np.zeros(0, dtype=np.float32)


if __name__ == "__main__":
    import wave
    text = sys.argv[1] if len(sys.argv) > 1 else "你咋才回我呀，我都等半天了。"
    t = GsvTts(device="cpu")
    t0 = time.time()
    a = t.synth_all(text)
    el = time.time() - t0
    print(f"音频 {len(a)/t.sr:.2f}s  合成 {el:.2f}s  RTF {el/max(len(a)/t.sr,1e-6):.2f}")
    out = r"D:\DHS\voice\out\gsv_engine_test.wav"
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(t.sr)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
    print("样本:", out)
