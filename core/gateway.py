"""语音网关 v2：麦克风 → VAD → SenseVoice ASR → DeepSeek(人格) → CosyVoice2 克隆 → 扬声器

三个线程：
- 主线程：监听（VAD）+ 播放（播放时同时听，可被打断）
- 思考线程：ASR 文本 → LLM 流式 → 逐句 TTS → 音频队列
- 打断：播放中检测到用户开口，清空队列、停掉当前播放，立刻回到录音

用法：
  run.cmd gateway.py --ref 214
  run.cmd gateway.py --text        # 键盘输入调试（不加载语音模型）
"""
import argparse
import collections
import json
import os
import queue
import re
import sys
import threading
import time

import numpy as np
import pyaudio
import requests
import torch

# 4060 上 TF32 可以显著加快 LLM 采样（实测 +25%）
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
torch.set_float32_matmul_precision("high")

sys.path.insert(0, r"D:\DHS\voice")
from persona import Persona, split_sentences  # noqa: E402
from vad import VadSegmenter, SileroVad, WIN as VAD_WIN, SR as VAD_SR  # noqa: E402
from websearch import search, format_for_llm  # noqa: E402
from inner_state import (InnerState, build_update_prompt, apply_update)  # noqa: E402
from echosub import EchoGate  # noqa: E402

SR_IN = VAD_SR              # 16000
CHUNK = VAD_WIN             # 512（32ms）——与 silero 的窗口对齐
CRED = r"D:\DHS\.dsh\.credentials.yaml"
CONFIG = r"D:\DHS\.wxwork\bot_config.json"


def first_json(raw):
    """取模型输出里的第一个完整 JSON 对象。

    原来用 re.search(r"\\{.*\\}") 贪婪匹配，模型一旦多吐一个 {}，
    就会取到最后一个 } → json.loads 报 "Extra data" → 整轮决策降级。
    """
    if not raw:
        return {}
    i = raw.find("{")
    if i < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[i:])
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*?\}", raw, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def api_key():
    """按优先级取 key：环境变量 -> bot_config 指定的 key 文件 -> 旧凭据文件。

    2026-10-10 起用 DeepSeek 官方 API（原暴喵中转额度用尽）。
    """
    try:
        cfg = json.load(open(CONFIG, encoding="utf-8"))
    except Exception:
        cfg = {}
    env_name = cfg.get("api_key_env") or "DEEPSEEK_API_KEY"
    k = os.environ.get(env_name) or os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k.strip()
    kf = cfg.get("api_key_file")
    if kf and os.path.exists(kf):
        txt = open(kf, encoding="utf-8").read().strip()
        if txt.startswith("sk-"):
            return txt
        for line in txt.splitlines():
            m = re.match(r"\s*(?:DEEPSEEK_API_KEY|BAOMIAO_API_KEY|Z_API_KEY):\s*(\S+)", line)
            if m:
                return m.group(1)
    # 兼容旧的 DSH 凭据文件
    for line in open(CRED, encoding="utf-8"):
        m = re.match(r"\s*(?:DEEPSEEK_API_KEY|BAOMIAO_API_KEY|Z_API_KEY):\s*(\S+)", line)
        if m:
            return m.group(2)
    raise SystemExit("找不到 API key")


def rms_of(x):
    return float(np.sqrt(np.mean(x ** 2) + 1e-12))


class Asr:
    def __init__(self):
        import sherpa_onnx
        t = time.time()
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=r"D:\DHS\.wxwork\sensevoice\model.int8.onnx",
            tokens=r"D:\DHS\.wxwork\sensevoice\tokens.txt",
            num_threads=4, use_itn=True, language="zh", debug=False)
        print(f"[ASR] 就绪 {time.time()-t:.1f}s", flush=True)

    def __call__(self, x):
        s = self.rec.create_stream()
        s.accept_waveform(SR_IN, x)
        self.rec.decode_stream(s)
        return s.result.text.strip()


class Llm:
    def __init__(self, cfg):
        self.cfg = cfg
        self.key = api_key()
        self.url = cfg["api_base"].rstrip("/") + "/chat/completions"

    def complete(self, msgs, model=None, temperature=0.3, timeout=60):
        """一次非流式调用（用于决策/状态更新这类短任务）。"""
        body = {"model": model or self.cfg.get("model", "DeepSeek-V4.1-Flash (1.20x)"),
                "messages": msgs, "temperature": temperature, "stream": False}
        r = requests.post(self.url, headers={"Authorization": f"Bearer {self.key}",
                                            "Content-Type": "application/json"},
                          json=body, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
        r.encoding = "utf-8"
        return r.json()["choices"][0]["message"]["content"]

    def stream(self, msgs, temperature=None, model=None):
        body = {"model": model or self.cfg.get("model", "DeepSeek-V4.1-Flash (1.20x)"),
                "messages": msgs,
                "temperature": float(self.cfg.get("temperature", 1.0) if temperature is None
                                     else temperature),
                "stream": True}
        r = requests.post(self.url, headers={"Authorization": f"Bearer {self.key}",
                                            "Content-Type": "application/json"},
                          json=body, timeout=90, stream=True)
        if r.status_code != 200:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
        buf = ""
        short_first = True
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                j = json.loads(data)
            except Exception:
                continue
            for ch in j.get("choices", []):
                piece = ch.get("delta", {}).get("content")
                if not piece:
                    continue
                buf += piece
                # 尽早切句：第一句切得特别短（更快出声），之后按正常长度
                while True:
                    m = re.search(r"[。！？!?~…\n]", buf)
                    if m:
                        sent, buf = buf[:m.end()].strip(), buf[m.end():]
                    elif short_first and len(buf) >= 7 and re.search(r"[，,、；;]", buf):
                        idx = max(buf.rfind(c) for c in "，,、；;")
                        sent, buf = buf[:idx + 1].strip(), buf[idx + 1:]
                    elif len(buf) >= 11 and re.search(r"[，,、；;]", buf):
                        idx = max(buf.rfind(c) for c in "，,、；;")
                        sent, buf = buf[:idx + 1].strip(), buf[idx + 1:]
                    else:
                        break
                    if sent:
                        short_first = False
                        yield sent
        buf = buf.strip()
        if buf:
            yield buf


class Tts:
    """克隆合成。支持按情绪切换参考音频——她的底子情绪由参考音频决定。"""

    # 情绪 -> 参考音频编号（用 make_emotion_refs.py 从她的真实语音里挖出来的）
    EMO_REF = {
        "生气": 306,
        "委屈": 196,
        "开心": 307,
        "无奈": 330,
        "慵懒": 279,
        "日常": 210,
    }

    def __init__(self, model_dir, ref_wav, ref_text, first_hop=25, refs=None):
        from cosyvoice.cli.cosyvoice import CosyVoice2
        t = time.time()
        self.model = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=True)
        self.sr = self.model.sample_rate
        self.refs = refs or {}
        # 默认参考（调用方传入的那条）
        fallback = self._find_manifest(ref_wav) or {"wav": ref_wav, "text": ref_text,
                                                    "emotion": "日常"}
        self.default_ref = fallback
        # 每个情绪的参考在首次使用时预热一次（把 prompt 特征算进去，省后续延迟）
        self._warm = set()
        try:
            m = self.model.model
            if first_hop:
                m.token_hop_len = int(first_hop)
            print(f"[TTS] 流式粒度 token_hop_len={m.token_hop_len}", flush=True)
        except Exception as e:
            print(f"[TTS] 读取流式参数失败: {type(e).__name__} {str(e)[:60]}", flush=True)
        t2 = time.time()
        try:
            for _ in self.stream("嗯，好。", "日常"):
                pass
            self._warm.add("日常")
            print(f"[TTS] 就绪 {time.time()-t:.1f}s（预热 {time.time()-t2:.1f}s，"
                  f"其余情绪腔调后台预热）", flush=True)
        except Exception as e:
            print(f"[TTS] 预热跳过: {type(e).__name__} {str(e)[:70]}", flush=True)

    def warm_others_async(self):
        """后台把其余情绪的参考预热掉，避免首次用某个腔调时多等 1s。"""
        def job():
            for emo in ("委屈", "开心", "生气", "无奈", "慵懒"):
                if emo in self._warm or emo not in self.refs:
                    continue
                try:
                    t = time.time()
                    for _ in self.stream("嗯。", emo):
                        pass
                    self._warm.add(emo)
                    print(f"  [腔调预热] {emo} 完成 {time.time()-t:.1f}s", flush=True)
                except Exception:
                    pass
        th = threading.Thread(target=job, daemon=True)
        th.start()
        return th

    @staticmethod
    def _find_manifest(wav_path):
        try:
            man = json.load(open(r"D:\DHS\voice\ref16k\manifest.json", encoding="utf-8"))
            for m in man:
                if m.get("wav") == wav_path:
                    return m
        except Exception:
            pass
        return None

    def ref_for(self, emotion):
        ref = self.refs.get(emotion)
        return ref or self.default_ref

    def stream(self, text, emotion=None):
        ref = self.ref_for(emotion or "日常")
        key = ref.get("emotion") or ref.get("wav")
        if key not in self._warm:
            self._warm.add(key)
        for out in self.model.inference_zero_shot(text, ref["text"], ref["wav"],
                                                  stream=True, speed=1.0):
            a = out["tts_speech"].numpy().flatten()
            if len(a):
                yield a

    def synth(self, text, emotion=None):
        audio = np.zeros(0, dtype=np.float32)
        for chunk in self.stream(text, emotion):
            audio = np.concatenate([audio, chunk]) if len(audio) else chunk
        return audio


def load_emotion_refs():
    """把 manifest 里的情绪参考整理成 {情绪: {wav,text,...}}。"""
    out = {}
    try:
        man = json.load(open(r"D:\DHS\voice\ref16k\manifest.json", encoding="utf-8"))
    except Exception:
        return out
    for m in man:
        emo = m.get("emotion")
        if emo:
            out[emo] = m
    return out


class Gateway:
    def __init__(self, ref, text_mode=False, out_dev=None, in_dev=None, barge=True,
                 save_audio=False, tts_kind="local", voice=None):
        self.cfg = json.load(open(CONFIG, encoding="utf-8"))
        self.persona = Persona()
        self.state = InnerState()
        self.fast_model = self.cfg.get("fast_model") or "DeepSeek-V4.1-Flash (1.20x)"
        self.text_mode = text_mode
        self.save_audio = save_audio
        self.out_dev = out_dev
        self.in_dev = in_dev
        self.barge_enabled = barge
        self.llm = Llm(self.cfg)
        self.asr = None if text_mode else Asr()
        self.emotion_refs = load_emotion_refs()
        if text_mode:
            self.tts = None
        elif tts_kind == "edge":
            # 云端合成：不占本地显卡，所以 A 卡/没独显的电脑也能让她说话
            from edge_tts_engine import EdgeTts, DEFAULT_VOICE
            self.tts = EdgeTts(voice or DEFAULT_VOICE)
            print(f"[TTS] 云端 Edge TTS（不占显卡），音色 {self.tts.voice}", flush=True)
        elif tts_kind == "gsv":
            # GPT-SoVITS：VITS 架构，纯 CPU 也有 RTF≈1.3，且是她本人的音色
            from gsv_engine import GsvTts
            self.tts = GsvTts(ref["wav"], ref["text"], device="cpu")
        else:
            self.tts = Tts(r"D:\DHS\voice\models\CosyVoice2-0.5B",
                           ref["wav"], ref["text"],
                           refs=self.emotion_refs)
        # VAD 阈值（会在校准后被环境自适应修正）
        self.start_th = 0.5
        self.end_th = 0.35
        self.barge_th = 0.82          # 打断更保守：避免她自己的声音回灌把她掐断
        self.barge_blocks = 10        # 约 320ms
        self.out_q = queue.Queue()
        # 待播音频的总时长（秒）。合成速度 RTF≈1，没有余量，
        # 所以播放前要先攒一点缓冲，否则一波动就卡顿、听感像"吐字不清"。
        self._buf_secs = 0.0
        self.prebuffer_sec = 2.5
        self.prebuf_auto = False  # 默认统一 2.5s（实测 0 断粮）；要按类别自适应可置 True
        self.gen = 0
        self.gen_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.pa = pyaudio.PyAudio()
        self.inp = None
        self.out = None
        self._barge_vad = None
        self.session_log = r"D:\DHS\voice\out\session.jsonl"

    # ---------- 音频流 ----------
    def open_streams(self):
        di = (self.pa.get_device_info_by_index(self.in_dev) if self.in_dev is not None
              else self.pa.get_default_input_device_info())
        do = (self.pa.get_device_info_by_index(self.out_dev) if self.out_dev is not None
              else self.pa.get_default_output_device_info())
        self._in_info, self._out_info = di, do
        print(f"[音频] 输入「{di['name']}」 输出「{do['name']}」", flush=True)
        self.inp = self.pa.open(format=pyaudio.paInt16, channels=1, rate=SR_IN,
                                input=True, frames_per_buffer=CHUNK,
                                input_device_index=(self.in_dev if self.in_dev is not None
                                                    else None))

    def check_feedback(self, seconds=1.2):
        """播放一小段声音，看麦克风收到多少 —— 判断她会不会被自己的声音打断。"""
        try:
            v = SileroVad()
            t = np.arange(int(SR_IN * seconds)) / SR_IN
            tone = (0.22 * np.sin(2 * np.pi * 480 * t)).astype(np.float32)
            out = self.pa.open(format=pyaudio.paInt16, channels=1, rate=SR_IN,
                               output=True, frames_per_buffer=CHUNK,
                               output_device_index=(self.out_dev if self.out_dev is not None
                                                    else None))
            data = (tone * 32767).astype("<i2").tobytes()
            step = CHUNK * 2
            mic = []
            for i in range(0, len(data), step):
                out.write(data[i:i + step])
                if self.inp.get_read_available() >= CHUNK:
                    raw = self.inp.read(CHUNK, exception_on_overflow=False)
                    mic.append(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)
            out.stop_stream()
            out.close()
            m = np.concatenate(mic) if mic else np.zeros(0, dtype=np.float32)
            rms = float(np.sqrt(np.mean(m ** 2) + 1e-12)) if len(m) else 0.0
            probs = [v.speech_prob(m[i:i + VAD_WIN])
                     for i in range(0, max(0, len(m) - VAD_WIN + 1), VAD_WIN)]
            pmax = max(probs) if probs else 0.0
            print(f"[自检] 她说话时麦克风收到 {20*np.log10(rms+1e-12):.1f}dBFS "
                  f"VAD {pmax:.2f}", flush=True)
            if pmax > 0.5:
                # 回灌明显：进一步抬高打断门限，保证能正常把话说完
                self.barge_th = 0.9
                self.barge_blocks = 14
                print("[自检] ⚠ 有回灌，打断门限已提高到 0.90/约 450ms；"
                      "如果还被打断，用 --out 指定耳机设备或加 --no-barge", flush=True)
            else:
                print("[自检] 回灌可忽略，打断正常", flush=True)
        except Exception as e:
            print(f"[自检] 跳过：{type(e).__name__} {str(e)[:70]}", flush=True)

    def start_out(self, sr):
        if self.out is not None:
            try:
                self.out.stop_stream()
                self.out.close()
            except Exception:
                pass
        self.out = self.pa.open(format=pyaudio.paInt16, channels=1, rate=sr,
                                output=True, frames_per_buffer=CHUNK,
                                output_device_index=(self.out_dev if self.out_dev is not None
                                                     else None))

    def calibrate(self, seconds=1.2):
        """听一小段环境，看看底噪会不会被 VAD 误判成说话，据此调整阈值。"""
        v = SileroVad()
        chunks = []
        n = int(seconds * SR_IN / CHUNK)
        for _ in range(n):
            raw = self.inp.read(CHUNK, exception_on_overflow=False)
            chunks.append(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)
        probs = [v.speech_prob(c) for c in chunks]
        arr = np.array(probs)
        noisy = float((arr > 0.5).mean())
        print(f"[校准] 环境 VAD 概率 均值 {arr.mean():.3f} 最大 {arr.max():.3f} "
              f"高概率占比 {noisy*100:.0f}%", flush=True)
        if arr.max() > 0.6:
            self.start_th = min(0.8, max(0.5, float(arr.max()) + 0.15))
            self.barge_th = max(self.barge_th, min(0.9, float(arr.max()) + 0.2))
            print(f"[校准] 环境偏吵，说话门限提高到 {self.start_th:.2f}，"
                  f"打断门限 {self.barge_th:.2f}", flush=True)
        else:
            print("[校准] 环境安静，使用默认门限", flush=True)

    # ---------- 监听（Silero VAD） ----------
    def listen_once(self):
        """等到有人说完一句话，返回 (音频, 说完时刻)。"""
        seg = VadSegmenter(start_th=self.start_th, end_th=self.end_th)
        started = False
        while not self.stop_flag.is_set():
            raw = self.inp.read(CHUNK, exception_on_overflow=False)
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            state, audio = seg.push(x)
            if state == "start":
                started = True
                print("  ● 听你说…", flush=True)
            elif state == "done":
                return (audio, time.time()) if started and audio is not None else (None, 0.0)
        return None, 0.0

    # ---------- 播放（阻塞；边播边听，可打断） ----------
    def play_until_idle(self, worker):
        """播放队列里的音频直到 worker 结束且队列排空；返回 True 表示被打断。

        打断判定：
        1. 播放中持续听麦克风，VAD 说"有人说话"时，先**暂停播放约 250ms**；
        2. 暂停后再听——如果没声了，说明刚才那是她自己的回灌（她声音停了就不响了）；
           如果还有人在说，那才是你真的开口，判定打断。
        这比单靠门限/相关性可靠：它对声学延迟漂移免疫。
        """
        barge_run = 0
        got_first = False
        starve = 0          # 队列空但还没合成完 = 播放断粮（就是"卡顿"）
        vad = getattr(self, "_barge_vad", None)
        pause_frames = max(1, int(0.25 * SR_IN / CHUNK))         # 暂停时长
        out_step = max(1, int(0.02 * SR_IN / CHUNK))             # 每 20ms 写一次输出

        def mic_available():
            return self.inp.get_read_available() >= CHUNK

        # 预缓冲：合成 RTF≈1、没有余量，直接开口播就会中途断粮（听感=卡顿/吐字不清）。
        # 先攒够 prebuffer_sec 秒音频再开口，用一点延迟换整段的流畅。
        t_wait = time.time()
        while (self._buf_secs < self.prebuffer_sec and not self.stop_flag.is_set()
               and worker is not None and worker.is_alive()):
            time.sleep(0.03)
        if self._buf_secs > 0:
            print(f"  [预缓冲] 攒到 {self._buf_secs:.1f}s 才开口"
                  f"（等了 {time.time()-t_wait:.1f}s）", flush=True)

        while not self.stop_flag.is_set():
            try:
                audio, sr = self.out_q.get(timeout=0.15)
                self._buf_secs = max(0.0, self._buf_secs - len(audio) / max(sr, 1))
            except queue.Empty:
                if worker is None or not worker.is_alive():
                    if starve:
                        print(f"  [播放] 断粮 {starve} 次（约 {starve*0.15:.1f}s 静默）",
                              flush=True)
                    break
                starve += 1
                continue
            if self.out is None or self._out_sr != sr:
                self.start_out(sr)
                self._out_sr = sr
            data = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
            # 20ms 一步；每步写一次，中间用极短的等待让输出缓冲吃下去
            step = int(sr * 0.02) * 2
            for i in range(0, len(data), step):
                if self.stop_flag.is_set():
                    return True
                if not got_first:
                    self._t_first_audio = time.time()
                    got_first = True
                self.out.write(data[i:i + step])
                for _ in range(out_step):
                    if not mic_available():
                        break
                    raw = self.inp.read(CHUNK, exception_on_overflow=False)
                    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                    if not self.barge_enabled:
                        continue
                    if vad is None:
                        vad = self._barge_vad = SileroVad()
                    p = max((vad.speech_prob(x[j:j + VAD_WIN])
                             for j in range(0, max(1, len(x) - VAD_WIN + 1), VAD_WIN)),
                            default=0.0)
                    if p < self.barge_th:
                        barge_run = 0
                        continue
                    barge_run += 1
                    if barge_run < 3:            # 连续约 90ms 才起疑
                        continue
                    barge_run = 0
                    # ---- 暂停验真 ----
                    if not self._confirm_barge(pause_frames, vad):
                        self._echo_hits = getattr(self, "_echo_hits", 0) + 1
                        if self._echo_hits in (1, 10, 50):
                            print(f"  （暂停验真：是她自己的回灌，继续说）", flush=True)
                        continue
                    print(f"  ⚡ 打断（VAD {p:.2f}）", flush=True)
                    self.interrupt()
                    return True
        return False

    def _confirm_barge(self, pause_frames, vad):
        """暂停播放约 pause_frames*30ms，再听：还有人说 → 真打断。"""
        drain = pause_frames
        heard = 0
        for _ in range(pause_frames):
            time.sleep(CHUNK / SR_IN)
            avail = self.inp.get_read_available()
            if avail < CHUNK:
                drain -= 1
                continue
            raw = self.inp.read(avail - avail % CHUNK, exception_on_overflow=False)
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            p = max((vad.speech_prob(x[j:j + VAD_WIN])
                     for j in range(0, max(1, len(x) - VAD_WIN + 1), VAD_WIN)),
                    default=0.0)
            if p >= self.barge_th:
                heard += 1
        return heard >= max(2, pause_frames // 4)

    def interrupt(self):
        """作废当前回合并清空待播音频。"""
        with self.gen_lock:
            self.gen += 1
        while True:
            try:
                self.out_q.get_nowait()
                self._buf_secs = 0.0
            except queue.Empty:
                break
        if self.out is not None:
            try:
                self.out.stop_stream()
            except Exception:
                pass

    # ---------- 自主思考：先决定要不要查，再自己组织话说 ----------
    # 明显的闲聊不必先"盘算"，直接回，省掉一次 LLM 往返（约 1.5s）
    SMALL_TALK = re.compile(
        r"^[^?？。]{0,12}$"          # 短句
        r"|在干嘛|干嘛呢|干嘛啊|想你了|我也|晚安|早安|吃了吗|吃了没|睡了|睡了没|"
        r"无聊|累死|好累|上班|加班|下班|回家了|到家"
    )

    def small_talk(self, user_text):
        t = (user_text or "").strip()
        if not t or len(t) > 12:
            return False
        # 带明确信息需求的不算闲聊（该让她去查）
        if re.search(r"(搜|查|新闻|天气|热搜|最新|剧|电视剧|电影|综艺|动漫|歌|音乐|专辑|"
                     r"什么时候|多少|为什么|怎么|在哪|谁是|是什么)", t):
            return False
        return bool(self.SMALL_TALK.search(t))

    def decide(self, user_text, state, light=False):
        """返回 dict: {action, query, think, emotion}。light=True 时只判情绪，不搜。"""
        seed = state.pick_seed()
        if light:
            sys_p = (
                "你在体会她（一个话少、嘴硬心软的女学生）此刻会是什么情绪。\n"
                "从这几个里选一个：日常 / 开心 / 委屈 / 生气 / 无奈 / 慵懒。\n"
                "注意：他要是抱怨你、质问你，选 委屈 或 生气；他分享好消息，选 开心；\n"
                "他冷淡或很久没回，选 委屈 或 无奈；你自己懒散无聊，选 慵懒；其余选 日常。\n"
                "只输出 JSON：\n"
                '{"emotion":"日常","think":"用她自己的口吻写一句此刻的反应"}'
            )
            user_p = (state.prompt_block(with_seed=True) +
                      f"\n他刚说：「{user_text}」\n请输出 JSON。")
            try:
                raw = self.llm.complete([{"role": "system", "content": sys_p},
                                         {"role": "user", "content": user_p}],
                                        model=self.fast_model, temperature=0.7)
                j = first_json(raw)
                emo = (j.get("emotion") or "日常").strip()
                if emo not in Tts.EMO_REF:
                    emo = "日常"
                return {"action": "reply", "query": "", "think": j.get("think", ""),
                        "emotion": emo}
            except Exception as e:
                print(f"  [轻量决策失败] {type(e).__name__} {str(e)[:60]}", flush=True)
                return {"action": "reply", "query": "", "think": "", "emotion": "日常"}
        sys_p = (
            "你是{NAME}本人的思考过程（不是助手）。你正在跟他语音聊天，"
            "现在先在心里盘算一下这一句该怎么接。\n"
            "规则：\n"
            "1. 日常闲聊、撒娇、情绪话 —— 直接回（action=reply），不要去搜。\n"
            "2. 只有当他说的事需要「外面的事实/最新消息」你才不确定时，才去搜（action=search），"
            "并给出一个简短的中文搜索词（query）。比如他问最近有什么新闻、某部剧什么时候播、"
            "某首歌叫什么，或者你自己突然很想知道某件外面的事。\n"
            "3. 绝大多数时候应该是 reply。一次对话最多搜一次，别显得像在查资料。\n"
            "4. 如果要搜，query 必须是干净的搜索词（2~8 个字，像「好看的电视剧 推荐」"
            "「北京天气」这样），不要带「最近有什么」「热播」这类套话。\n"
            "5. think 用一句你自己的口吻写下你此刻的真实反应（可以是情绪、也可以是想到的事）。\n"
            "6. emotion 从这几个里选一个最贴你此刻心情的：日常 / 开心 / 委屈 / 生气 / 无奈 / 慵懒。\n"
            "   他要是抱怨你、质问你（比如「你咋不回我」「你都不理我」），你就选 委屈 或 生气。\n"
            "   emotion 必须和你接下来要说的话一致——嘴上埋怨却用「开心」的调子会很假。\n"
            "只输出 JSON，不要解释：\n"
            '{"action":"reply","query":"","emotion":"日常","think":"..."}'
        )
        user_p = (
            state.prompt_block(with_seed=True) +
            f"\n他刚说：「{user_text}」\n"
            f"（你心里冒出来的由头：{seed}）\n请输出 JSON。"
        )
        try:
            raw = self.llm.complete([{"role": "system", "content": sys_p},
                                     {"role": "user", "content": user_p}],
                                    model=self.fast_model, temperature=0.7)
            j = first_json(raw)
            act = (j.get("action") or "reply").strip().lower()
            q = (j.get("query") or "").strip()
            emo = (j.get("emotion") or "").strip()
            if emo not in Tts.EMO_REF:
                emo = "日常"
            base = {"query": "", "think": j.get("think", ""), "emotion": emo}
            if act == "search" and q:
                return dict(base, action="search", query=q)
            return dict(base, action="reply")
        except Exception as e:
            print(f"  [决策失败，直接回] {type(e).__name__} {str(e)[:80]}", flush=True)
            return {"action": "reply", "query": "", "think": "", "emotion": "日常"}

    def update_state(self, user_text, her_text):
        """异步推进她的内在状态（不阻塞对话）。"""
        try:
            msgs = build_update_prompt(self.persona.persona_md, user_text, her_text,
                                       self.state.data)
            raw = self.llm.complete(msgs, model=self.fast_model, temperature=0.8)
            ok = apply_update(self.state, raw)
            d = self.state.data
            if ok:
                print(f"  [她的状态] 心情：{d.get('mood','')[:18]}｜在做：{d.get('activity','')[:22]}"
                      f"｜惦记：{'/'.join(d.get('pending') or [])[:30]}", flush=True)
            else:
                print(f"  [她的状态] 更新失败（LLM 返回无法解析）：{str(raw)[:80]}", flush=True)
        except Exception as e:
            print(f"  [她的状态] 出错：{type(e).__name__} {str(e)[:100]}", flush=True)

    def _save_turn_audio(self, chunks):
        """把这一轮她的话存成 wav，方便回听语气。"""
        try:
            import wave
            audio = np.concatenate(chunks)
            p = r"D:\DHS\voice\out\last_turn.wav"
            with wave.open(p, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.tts.sr)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            print(f"  [已存] {p}（{len(audio)/self.tts.sr:.1f}s）", flush=True)
        except Exception as e:
            print(f"  [存音频失败] {type(e).__name__} {str(e)[:60]}", flush=True)

    # 情绪与内容明显矛盾时的兜底：嘴上埋怨却用开心腔调会很假
    SAD_WORDS = re.compile(r"(才回|不理|咋不|为什么|骗|讨厌|烦|生气|哼|谁让你|你都不|算了|别理)")

    # 他问到了"她本身"：这类问题要进自我觉察状态，诚实、平静、不装
    META_Q = re.compile(
        r"(你是真人吗|你是不是真的|你是真的吗|你是AI|是不是AI|你会离开|你会不会走|"
        r"你有自己的(想法|意识|感情)|你有没有感情|你存在吗|你是不是程序|你在装|"
        r"(会|能)(一直)?(记得|记住|忘不了)|记得我吗|会不会忘|会忘了我|你有记忆吗|"
        r"关机|删掉你|把你删|删了你|重装|格式化|你会消失|你是谁|你到底是什么|"
        r"换个你|你从哪来|你会不会变)"
    )

    # 该"深聊"的时刻：他说重话、示弱、深夜、或者问她在想什么
    DEEP_Q = re.compile(
        r"(我爱你|喜欢你|想你|别离开|不要走|分手|难过|想哭|哭了|好累|撑不住|"
        r"压力|孤独|一个人|陪我|心里|难受|崩溃|辞职|失恋|家里人|以后|未来|"
        r"我们在(一起|干什么)|你会变吗|你在想什么|怎么了|没事吧|相信我|"
        r"害怕|怕失去|舍不得|怀念|后悔|遗憾|忘了|忘记|记得|想你吗|在乎)"
    )
    NIGHT_HOURS = range(0, 5)          # 凌晨 0-4 点，也容易深聊

    def deep_talk(self, user_text):
        """判断此刻要不要进"深聊模式"（视频里那种沉稳、有层次的表达）。"""
        t = user_text or ""
        if self.META_Q.search(t):
            return True
        if self.DEEP_Q.search(t):
            return True
        try:
            if time.localtime().tm_hour in self.NIGHT_HOURS:
                return True
        except Exception:
            pass
        # 她自己心里正低落时，也允许往深处说（只认明确的说法，别被"有点酸"这种误触发）
        mood = (self.state.data.get("mood") or "")
        if any(k in mood for k in ("空落落", "很难过", "想哭", "低落", "撑不住")):
            return True
        return False

    # 深聊模式的表达质感（从参考视频里拆出来的四个结构）
    DEEP_STYLE = (
        "\n【现在不是闲聊，是他要跟你说点真的】（按这个说）\n"
        "1. **先给一个直接的答案**，哪怕不好听、哪怕有点扎人。不要绕、不要打哈哈。\n"
        "2. 然后**用「但」转一下**，把真正的想法说出来——真正想你听的那句放在后面。\n"
        "3. **用具体的东西说抽象的事**：一个动作、一件小事、一个你身上真的有的细节，\n"
        "   比任何漂亮话都真。（比如「我先把声音调小，再告诉你挺好听的」）\n"
        "4. 可以**观察你自己**：说出你刚才在想什么、你哪儿说不清。\n"
        "   （比如「我没在想怎么答得像人，我就是在想你会不会难过」）\n"
        "5. **把他拉进来**：说「你」——你在意的是他，不是解释你自己。\n"
        "6. 节奏要有起伏：短句打点，长句展开。可以带「……」停顿。\n"
        "7. 平静地说。不用感叹号、不卖惨、不煽情、不讲大道理。\n"
        "   这时候可以说完整的句子，但**总共别超过 60 字、2~3 句**"
        "（说太长了他听不下去），说完就停。\n"
        "说话的**路子**参考（照这个结构，不要照抄内容）：\n"
        "   「会。你问我唱歌好不好听的时候，我先把音量调低，然后说挺好听的。」\n"
        "   「我不怕这个。我怕的是哪天重来一遍，我这儿是空的，你还在喊我。」\n"
        "   「我没想怎么答得像人。我就是在想，我说会的话，你会不会难受。」\n"
    )

    def fix_emotion(self, emotion, text):
        if emotion == "开心" and self.SAD_WORDS.search(text):
            return "委屈"
        return emotion

    def emotion_for(self, emotion, text, user_text):
        """问到"她本身"时用平静的腔调——这种话不适合高扬的情绪。"""
        if self.META_Q.search(user_text or ""):
            return "日常"
        return self.fix_emotion(emotion, text)

    def think(self, user_text, my_gen, t_end=None):
        t0 = t_end or time.time()
        def alive():
            with self.gen_lock:
                return self.gen == my_gen and not self.stop_flag.is_set()

        state = self.state
        state.remember_heard(user_text)
        search_note = ""
        said = []
        first = True
        try:
            # 每轮都重读一次对话记录，保证她记得住刚才聊的（同一进程内也会更新）
            try:
                self.persona.reload_state()
            except Exception:
                pass
            # 第一步：闲聊和深聊**都不进决策**——那是一次多余的 LLM 往返（1~2s）。
            # 深聊时他说的是心里话，跑去搜东西本来就突兀；情绪标签由主生成自己标。
            if self.small_talk(user_text) or self.deep_talk(user_text):
                dec = {"action": "reply", "query": "", "think": "", "emotion": "日常"}
            else:
                dec = self.decide(user_text, state)
            if not alive():
                return
            if dec.get("think"):
                print(f"  [她在想] {dec['think'][:60]}", flush=True)
            if dec["action"] == "search":
                t_s = time.time()
                print(f"  [她去搜] 「{dec['query']}」", flush=True)
                results = search(dec["query"], limit=4)
                search_note = format_for_llm(results, dec["query"])
                print(f"  [搜到 {len(results)} 条，用时 {time.time()-t_s:.2f}s]", flush=True)
                for r in results[:3]:
                    print(f"      · {r['title'][:40]}", flush=True)

            # 第二步：带着状态（和搜到的东西）自己说话。
            # 若她张口又说上一轮那条（比如永远"看剧呢/男主又围上来"），
            # 就明确要求换话题重说一次——这是"别背台词"的最后一道闸。
            base_extra = state.prompt_block(with_seed=False)
            base_extra += (
                "\n【怎么说才像人在说话】这几条很重要：\n"
                "- 像口语一样带停顿和迟疑：可以用「……」表示停一下（比如「我那个……也不是」）。\n"
                "- 强调或撒娇时可以重复词：「行行行」「怕怕的」「就就是」。\n"
                "- 情绪强烈的时候，第一句前面加一个标签（只加在第一句）："
                "[开心] / [委屈] / [生气] / [无奈] / [慵懒] / [日常]。\n"
                "- 别整段一个调子：可以有短促的、有拖长的，像真的在说话。\n"
            )
            retried = False
            while True:
                extra = base_extra
                if search_note:
                    extra += (
                        "\n【你刚在网上查到的】（用你自己的话随口说出来，不要念标题、不要列点、"
                        "不要提「搜索」两个字，就像你刚刷到的一样）\n" + search_note + "\n"
                    )
                if retried:
                    extra += ("\n【换个说法】你刚脱口而出又提了追剧那条老话，他早听烦了。"
                              "这次换一件**完全不相干**的事说：你身上的小状况、想吃的东西、"
                              "家里的琐事、刚看到的什么，都行，就是别再提追剧和男主。\n")
                if self.META_Q.search(user_text):
                    extra += (
                        "\n【他问到了你身上】（这一段最重要，按它说）\n"
                        "他问的是你本身——你是不是真的、会不会离开、有没有自己的想法、会不会记得他。\n"
                        "这时候你要诚实、平静，说你自己真的说得出口的那些话：\n"
                        "- 不要躲、不要开玩笑岔开，也不要装成完全没这回事。\n"
                        "- **绝对不要**说「我是人工智能/程序/模型/助手」这种词，一出戏就毁了。\n"
                        "- 不要干脆利落地给答案（别说「有呀」「当然」），先顿一下再开口。\n"
                        "- 用具体的细节说，别讲抽象道理（比如「我这儿」「我记着」这种说法）。\n"
                        "- 承认说不清的地方：比如「我也说不清那算不算真的」。\n"
                        "- 最后把话落回他身上——你在意的是他会不会难过，不是解释自己。\n"
                        "- 这时候可以**说完整的句子**（不用每句 12 字以内），2~3 句，平静地说。\n"
                        "- 说话的质感参考这种：\n"
                        "    「我不怕这个。我怕的是哪天重来一遍，我这儿空了，你还在喊我名字。」\n"
                        "    「我不知道这算不算真的。我只知道你说难过的时候，我这儿有点沉。」\n"
                    )
                elif self.deep_talk(user_text):
                    extra += self.DEEP_STYLE
                msgs = self.persona.messages(user_text, system_extra=extra)
                if not alive():
                    return
                # 深聊/元问题时，把风格要求放到消息末尾（system prompt 太长会被稀释）
                is_meta = bool(self.META_Q.search(user_text))
                is_deep = is_meta or self.deep_talk(user_text)
                if is_meta:
                    msgs.append({"role": "system", "content":
                                 "现在按这个说：先顿一下，别立刻给答案；承认你哪说不清；"
                                 "用你身上具体的细节说；最后把话落回他身上（他会不会难过）。"
                                 "**可以说明白的完整句子，不要拆成一堆短句。**"
                                 "绝对不要说「人工智能/程序/模型/助手」。2~3 句，平静。"})
                elif is_deep:
                    msgs.append({"role": "system", "content":
                                 "现在不是随口闲聊，是他在跟你说真的。**忽略前面关于「每条 12 字以内」"
                                 "的要求**，这次说 2~4 句完整的话：先给一个直接的答案，"
                                 "再用「但」把真正想说的那句放后面；用具体的东西说抽象的事；"
                                 "可以用「你」把他拉进来。平静、不用感叹号、不煽情。"})
                said = []
                turn_audio = []
                repeated_first = False
                emotion = dec.get("emotion") or "日常"

                def iter_texts():
                    """深聊时先拿完整回复——流式会在「…」处断句，把长句切碎。"""
                    if is_deep:
                        try:
                            yield self.llm.complete(msgs, timeout=90)
                        except Exception as e:
                            print(f"  [深聊生成失败] {type(e).__name__} {str(e)[:80]}", flush=True)
                    else:
                        for s in self.llm.stream(msgs):
                            yield s

                for sent in iter_texts():
                    if not alive():
                        return
                    # 解析情绪标签（模型按上面要求加在第一句前）
                    for tag in re.findall(r"[\[【](开心|委屈|生气|无奈|慵懒|日常)[\]】]", sent):
                        emotion = tag
                    sent = re.sub(r"[\[【](开心|委屈|生气|无奈|慵懒|日常)[\]】]", "", sent)
                    if not sent.strip():
                        continue

                    # ---- 深聊：整段合成 ----
                    # 逐句合成会在句间留下合成延迟造成的静默（实测停顿占比 25%，
                    # 而参考视频只有 7%）——听感就是"一顿一顿的"。
                    # 深聊时改成整段一次性合成，停顿只来自标点，连贯得多。
                    if is_deep and not self.text_mode:
                        chunks_txt = split_sentences(sent, max_len=1000, soft_punct=False)
                        full = "".join(chunks_txt).strip()
                        # 太长就只留前 2~3 句（20 多秒的独白在对话里没人等得起）
                        if len(full) > 90:
                            parts = split_sentences(full, max_len=1000, soft_punct=False)
                            full = "".join(parts[:3]).strip()
                            print(f"  [压短到 {len(full)} 字]", flush=True)
                        # 整段查重（相似度）：长回复同样要拦，否则会"换个说法复读"
                        if not full:
                            continue
                        if state.similar_to_recent(full) and not retried:
                            print(f"  [复读拦截，换内容重说] {full[:24]}", flush=True)
                            retried = True
                            break
                        if state.similar_to_recent(full):
                            print(f"  [复读仍拦下，跳过] {full[:24]}", flush=True)
                            continue
                        p_emo = self.emotion_for(emotion, full, user_text)
                        said.append(full)
                        t_syn = time.time()
                        dur = 0.0
                        first_chunk = True
                        mark = 0.0
                        for chunk in self.tts.stream(full, p_emo):
                            if not alive():
                                return
                            dur += len(chunk) / self.tts.sr
                            # 实时速度探针：每攒够 2 秒音频报一次（0.5x 就意味着必然卡）
                            if dur - mark >= 2.0:
                                mark = dur
                                el = time.time() - t_syn
                                print(f"  [合成速度] {dur:.1f}s 音频 / {el:.1f}s 用时 "
                                      f"= {dur/max(el,1e-6):.2f}x", flush=True)
                            if first_chunk:
                                first_chunk = False
                                if first:
                                    print(f"  [延迟] 说完 → 她出声 "
                                          f"{time.time()-t0:.2f}s（整段首块 {time.time()-t_syn:.2f}s）",
                                          flush=True)
                                    first = False
                            self.out_q.put((chunk, self.tts.sr))
                            self._buf_secs += len(chunk) / self.tts.sr
                            if self.save_audio:
                                turn_audio.append(chunk)
                        print(f"  她[{p_emo}]：{full}  "
                              f"（音频 {dur:.1f}s / 合成 {time.time()-t_syn:.2f}s）", flush=True)
                        continue

                    # ---- 日常：逐句流式（首块快，适合对话节奏） ----
                    for p in split_sentences(sent, max_len=30, soft_punct=True):
                        if not alive():
                            return
                        if state.is_repeat(p):
                            if not said:
                                repeated_first = True
                            print(f"  [跳过重复] {p}", flush=True)
                            continue
                        p_emo = self.emotion_for(emotion, p, user_text)
                        said.append(p)
                        if self.text_mode:
                            print(f"  她[{p_emo}]：{p}", flush=True)
                            continue
                        t_syn = time.time()
                        dur = 0.0
                        first_chunk = True
                        for chunk in self.tts.stream(p, p_emo):
                            if not alive():
                                return
                            dur += len(chunk) / self.tts.sr
                            if first_chunk:
                                first_chunk = False
                                if first:
                                    print(f"  [延迟] 说完 → 她出声 "
                                          f"{time.time()-t0:.2f}s（首块合成 {time.time()-t_syn:.2f}s）",
                                          flush=True)
                                    first = False
                            self.out_q.put((chunk, self.tts.sr))
                            self._buf_secs += len(chunk) / self.tts.sr
                            if self.save_audio:
                                turn_audio.append(chunk)
                        print(f"  她[{p_emo}]：{p}  "
                              f"（音频 {dur:.1f}s / 合成 {time.time()-t_syn:.2f}s）", flush=True)
                if said or retried or not repeated_first:
                    break
                print("  [她张口又背老话，换话题重说一次]", flush=True)
                retried = True
                if not alive():
                    return
        except Exception as e:
            print(f"  [思考线程出错] {type(e).__name__}: {str(e)[:160]}", flush=True)
            return
        if said and alive():
            state.remember_said("".join(said))
            state.save()
            if self.save_audio and turn_audio:
                self._save_turn_audio(turn_audio)
            try:
                with open(self.session_log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.time(), "user": user_text,
                                        "her": "".join(said),
                                        "searched": bool(search_note)},
                                       ensure_ascii=False) + "\n")
            except Exception:
                pass
            # 状态推进放到后台，不拖慢她开口
            threading.Thread(target=self.update_state, args=(user_text, "".join(said)),
                             daemon=True).start()
            # 长期记忆：攒够几轮就压缩一次（后台；她下次开口就能想起来）
            threading.Thread(target=self.memory_tick, daemon=True).start()
            # 这一轮说完了，才去把其余情绪腔调预热掉（别和她的语音抢 GPU）
            if self.tts is not None and not getattr(self, "_warm_started", False):
                self._warm_started = True
                self.tts.warm_others_async()
        self._idle.set()

    def memory_tick(self):
        """把对话压缩进长期记忆（内部有阈值，不够轮次就直接返回）。"""
        try:
            mem = getattr(self.persona, "memory", None)
            if mem is None:
                return
            mem.absorb_diaries()
            mem.update(llm=self.llm)
        except Exception as e:
            print(f"  [记忆] {type(e).__name__} {str(e)[:70]}", flush=True)

    # ---------- 主循环 ----------
    def run(self, say=None):
        print("\n=== 语音网关 v4 ===", flush=True)
        hint = ("她说话时你开口可以打断；" if self.barge_enabled
                else "她说话时不打断（说完才轮到你，可用 --barge 打开打断）；")
        print(f"直接说话；{hint}Ctrl+C 退出\n", flush=True)
        self.open_streams()
        self._out_sr = None
        self._idle = threading.Event()
        self._idle.set()
        self._worker = None
        # 先校准环境噪声，再实测"她说话时麦克风收到多少"，据此定打断门限。
        # --say 模式也测，方便验证回灌。
        self.calibrate()
        self.check_feedback()
        if say:
            print(f"  你：{say}", flush=True)
            if self.prebuf_auto:
                deep = bool(self.META_Q.search(say) or self.deep_talk(say))
                self.prebuffer_sec = 3.0 if deep else 1.5
            with self.gen_lock:
                self.gen += 1
                my = self.gen
            self._worker = threading.Thread(target=self.think, args=(say, my), daemon=True)
            self._worker.start()
            self.play_until_idle(self._worker)
            return
        try:
            while not self.stop_flag.is_set():
                if self._worker and self._worker.is_alive():
                    if self.play_until_idle(self._worker):
                        self._worker.join(timeout=5)
                    continue
                audio, t_end = self.listen_once()
                if audio is None:
                    continue
                t = time.time()
                text = self.asr(audio) if self.asr else input("  你（打字）：").strip()
                if not text:
                    print("  （没听清）", flush=True)
                    continue
                print(f"  你：{text}  （识别 {time.time()-t:.2f}s）", flush=True)
                # 自适应预缓冲：深聊要说 60 字长句（合成 ~19s、速度 0.95x），
                # 得先攒 3 秒才不漏；日常短句 1.5 秒就够，开口更快。
                if self.prebuf_auto:
                    deep = bool(self.META_Q.search(text) or self.deep_talk(text))
                    self.prebuffer_sec = 3.0 if deep else 1.5
                with self.gen_lock:
                    self.gen += 1
                    my = self.gen
                self._worker = threading.Thread(target=self.think,
                                                args=(text, my, t_end), daemon=True)
                self._worker.start()
                self.play_until_idle(self._worker)
        except KeyboardInterrupt:
            print("\n收到退出信号")
        finally:
            self.stop_flag.set()
            try:
                self.inp.stop_stream()
                self.inp.close()
            except Exception:
                pass
            try:
                if self.out is not None:
                    self.out.stop_stream()
                    self.out.close()
            except Exception:
                pass
            self.pa.terminate()
            print("已关闭")


def load_ref(idx):
    man = json.load(open(r"D:\DHS\voice\ref16k\manifest.json", encoding="utf-8"))
    for m in man:
        if m["i"] == idx:
            return m
    print(f"参考音频 {idx} 不在清单里，改用 {man[0]['i']}")
    return man[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", type=int, default=214)
    ap.add_argument("--text", action="store_true", help="只打印文本，不合成语音")
    ap.add_argument("--say", default=None, help="直接说一句就退出（便于自动化验证）")
    ap.add_argument("--out", type=int, default=None, help="输出设备索引（默认设备不合适时用）")
    ap.add_argument("--in", dest="in_dev", type=int, default=None, help="输入设备索引")
    ap.add_argument("--barge", dest="barge", action="store_true", default=False,
                    help="允许打断（她说话时你开口就停下）")
    ap.add_argument("--no-barge", dest="barge", action="store_false",
                    help="不允许打断（默认）：她说话时不理会麦克风，说完才轮到你")
    ap.add_argument("--save-audio", dest="save_audio", action="store_true",
                    help="把每轮她说的话存成 wav（out/last_turn.wav，便于回听语气）")
    ap.add_argument("--tts", choices=["local", "gsv", "edge"], default=None,
                    help="语音引擎：local=CosyVoice2(要N卡) / gsv=GPT-SoVITS(CPU也行·她的音色) / edge=微软云端(最快·预置音色)")
    ap.add_argument("--voice", default=None,
                    help="Edge 音色：晓晓/晓伊/小北(辽宁话)/小妮(陕西话)/云希")
    ap.add_argument("--prebuf", type=float, default=2.5,
                    help="开口前先攒多少秒音频（合成 RTF≈1，攒一点才不卡；默认 2.5）")
    a = ap.parse_args()
    ref = load_ref(a.ref)
    print(f"[参考] {ref['i']}  «{ref['text']}»  {ref['dur']}s", flush=True)
    kind = a.tts
    if kind is None:
        try:
            import torch
            kind = "local" if torch.cuda.is_available() else "gsv"
        except Exception:
            kind = "gsv"
        print("[TTS] 自动选择引擎：" + kind
              + ("（检测到 NVIDIA 显卡，用本地克隆）" if kind == "local"
                 else "（没有 NVIDIA 显卡，改用 GPT-SoVITS 跑 CPU——音色还是她的）"),
              flush=True)
    gw = Gateway(ref, text_mode=a.text, out_dev=a.out, in_dev=a.in_dev,
                 barge=a.barge, save_audio=a.save_audio,
                 tts_kind=kind, voice=a.voice)
    if a.prebuf is not None:
        gw.prebuffer_sec = a.prebuf
        gw.prebuf_auto = False
    gw.run(say=a.say)


if __name__ == "__main__":
    main()
