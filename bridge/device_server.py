"""设备服务：给桌面设备的 ESP32 用的 WebSocket 音频 + 状态通道。

设备侧要做的（固件）：
  I2S 麦克风采集 → 每 32ms 发一块 PCM16(16k 单声道) → 本服务
  本服务 → 判定说完 → ASR → LLM(她的人格) → 克隆 TTS → PCM16(24k) 分块回传
  设备 → I2S 功放播放；同时屏幕显示状态/对话/天数

协议：
  设备 → 服务  二进制帧: PCM16 LE, 16kHz, mono（每块建议 512 样本 = 32ms）
  服务 → 设备  二进制帧: PCM16 LE, 24kHz, mono（她的声音）
  服务 → 设备  文本帧 JSON:
      {"type":"hello","days":1,"portrait":"/portrait.png"}
      {"type":"state","value":"listening|thinking|speaking|idle"}
      {"type":"say","role":"you|her","text":"..."}
      {"type":"turn","days":1}

静态图（立绘/日记）用同端口的 HTTP 提供：http://<本机IP>:8766/portrait.png

用法：
  run.cmd device_server.py                 # 监听 0.0.0.0:8765
  run.cmd device_server.py --port 9000
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, r"D:\DHS\voice")

import websockets  # noqa: E402

from gateway import Asr, Tts, Llm, CONFIG, api_key  # noqa: E402
from persona import Persona, split_sentences  # noqa: E402
from inner_state import InnerState, build_update_prompt, apply_update  # noqa: E402
from vad import VadSegmenter  # noqa: E402

SR_IN = 16000
REF_WAV = r"D:\DHS\voice\ref16k\ref_210_16k.wav"
REF_TEXT = "没事，反正也不是我唱的。"
STATIC_DIR = r"D:\DHS\voice\out"
STATE_PATH = r"D:\DHS\voice\out\diary_state.json"
SESSION_LOG = r"D:\DHS\voice\out\session_device.jsonl"


def days_with_you():
    try:
        st = json.load(open(STATE_PATH, encoding="utf-8"))
        s = st.get("start_date")
        if s:
            from datetime import datetime
            d0 = datetime.strptime(s, "%Y-%m-%d").date()
            return (datetime.now().date() - d0).days + 1
    except Exception:
        pass
    return 1


def start_static(port):
    """同机提供立绘/日记图片给设备下载。"""
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=STATIC_DIR, **kw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv


class Engine:
    """共享的推理引擎（所有设备连接共用一个，省显存）。"""

    def __init__(self):
        t = time.time()
        self.cfg = json.load(open(CONFIG, encoding="utf-8"))
        self.asr = Asr()
        self.tts = Tts(r"D:\DHS\voice\models\CosyVoice2-0.5B", REF_WAV, REF_TEXT)
        self.llm = Llm(self.cfg)
        self.fast_model = self.cfg.get("fast_model") or "deepseek-chat"
        self.persona = Persona()
        self.state = InnerState()
        print(f"[引擎] 就绪 {time.time()-t:.1f}s", flush=True)
        self.lock = threading.Lock()          # 推理串行化（GPU 一次一件事）

    def think(self, user_text):
        """同步：文本 → 她的回复（逐句）。返回 [(句子, 情绪), ...]"""
        with self.lock:
            state = self.state
            state.remember_heard(user_text)
            msgs = self.persona.messages(user_text, system_extra=state.prompt_block())
            out = []
            said = []
            for sent in self.llm.stream(msgs):
                for p in split_sentences(sent, max_len=30):
                    if state.is_repeat(p):
                        continue
                    out.append((p, "日常"))
                    said.append(p)
            if said:
                state.remember_said("".join(said))
                state.save()
            return out

    def synth(self, text, emotion="日常"):
        with self.lock:
            audio = np.zeros(0, dtype=np.float32)
            for chunk in self.tts.stream(text, emotion):
                audio = np.concatenate([audio, chunk]) if len(audio) else chunk
            return audio, self.tts.sr

    def asr_text(self, pcm16k):
        with self.lock:
            return self.asr(pcm16k)

    def update_state(self, user_text, her_text):
        try:
            with self.lock:
                msgs = build_update_prompt(self.persona.persona_md, user_text, her_text,
                                           self.state.data)
                raw = self.llm.complete(msgs, model=self.fast_model, temperature=0.8)
                apply_update(self.state, raw)
        except Exception:
            pass


ENGINE = None          # 延迟到 main 里初始化


class Session:
    def __init__(self, ws, loop):
        self.ws = ws
        self.loop = loop
        self.seg = VadSegmenter()
        self.buf = []
        self.running = True

    async def send_text(self, **kw):
        try:
            await self.ws.send(json.dumps(kw, ensure_ascii=False))
        except Exception:
            self.running = False

    async def send_state(self, v):
        await self.send_text(type="state", value=v)

    async def run(self):
        await self.send_text(type="hello", days=days_with_you(),
                             portrait="/portrait.png", sample_rate=24000)
        print(f"[设备] 连接 {self.ws.remote_address}", flush=True)
        async for msg in self.ws:
            if isinstance(msg, bytes):
                await self.on_audio(msg)
            else:
                try:
                    j = json.loads(msg)
                except Exception:
                    continue
                if j.get("type") == "ping":
                    await self.send_text(type="pong", t=time.time())

    async def on_audio(self, data):
        """收到一块 PCM16 16k，喂 VAD；判定说完就跑一轮对话。"""
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        # VAD 按 512 样本窗口推进
        for i in range(0, len(x) - 511, 512):
            st, audio = self.seg.push(x[i:i + 512])
            if st == "start":
                await self.send_state("listening")
            elif st == "done" and audio is not None:
                self.loop.create_task(self.handle_turn(audio))

    async def handle_turn(self, audio16k):
        try:
            await self.send_state("thinking")
            text = await asyncio.to_thread(ENGINE.asr_text, audio16k)
            if not text:
                await self.send_state("idle")
                return
            await self.send_text(type="say", role="you", text=text)
            print(f"  你（设备）：{text}", flush=True)
            lines = await asyncio.to_thread(ENGINE.think, text)
            if not lines:
                await self.send_state("idle")
                return
            await self.send_state("speaking")
            said = []
            for p, emo in lines:
                await self.send_text(type="say", role="her", text=p)
                print(f"  她：{p}", flush=True)
                said.append(p)
                audio, sr = await asyncio.to_thread(ENGINE.synth, p, emo)
                if len(audio) == 0:
                    continue
                pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                step = int(sr * 0.05) * 2        # 每 50ms 一包
                for i in range(0, len(pcm), step):
                    if not self.running:
                        return
                    await self.ws.send(pcm[i:i + step])
                    await asyncio.sleep(0.02)    # 给设备一点缓冲
            try:
                with open(SESSION_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.time(), "user": text,
                                        "her": "".join(said)}, ensure_ascii=False) + "\n")
            except Exception:
                pass
            asyncio.create_task(asyncio.to_thread(ENGINE.update_state, text, "".join(said)))
            await self.send_text(type="turn", days=days_with_you())
            await self.send_state("idle")
        except Exception as e:
            print(f"  [设备会话出错] {type(e).__name__} {str(e)[:120]}", flush=True)
            await self.send_state("idle")


async def handler(ws):
    s = Session(ws, asyncio.get_running_loop())
    try:
        await s.run()
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        s.running = False
        print(f"[设备] 断开 {ws.remote_address}", flush=True)


async def main():
    global ENGINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--http-port", type=int, default=8766)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()

    print("[启动] 加载引擎（约 20 秒）...", flush=True)
    ENGINE = await asyncio.to_thread(Engine)
    start_static(a.http_port)
    print(f"[启动] 静态图: http://<本机IP>:{a.http_port}/portrait/official_main.png",
          flush=True)
    print(f"[启动] WebSocket: ws://<本机IP>:{a.port}/", flush=True)
    async with websockets.serve(handler, a.host, a.port, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
