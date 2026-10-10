"""Silero VAD 包装（onnxruntime，16kHz，512 采样窗口）。

比纯能量门限可靠得多：能区分「有人在说话」和「风扇/视频/街道噪声」。
"""
import numpy as np
import onnxruntime as ort

MODEL = r"D:\DHS\.cache\models\silero_vad.onnx"
SR = 16000
WIN = 512          # 16k 下 silero 的窗口
CTX = 64           # 额外上下文


class SileroVad:
    def __init__(self, model_path=MODEL, threads=2):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(model_path, sess_options=opts,
                                         providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.ctx = np.zeros((1, CTX), dtype=np.float32)

    def speech_prob(self, chunk):
        """chunk: float32，长度 512（不足则补零）。返回语音概率。"""
        if len(chunk) < WIN:
            chunk = np.pad(chunk, (0, WIN - len(chunk)))
        x = np.concatenate([self.ctx, chunk.reshape(1, -1).astype(np.float32)], axis=1)
        out, self.state = self.sess.run(
            None, {"input": x, "state": self.state, "sr": np.array(SR, dtype=np.int64)})
        self.ctx = x[:, -CTX:]
        return float(out[0][0])


class VadSegmenter:
    """把连续音频流切成一句一句。

    策略：概率高于 start_th 且持续 min_speech 才算开始；
          概率低于 end_th 且持续 min_silence 才算说完。
    """

    def __init__(self, start_th=0.5, end_th=0.35, min_speech_sec=0.25,
                 min_silence_sec=0.6, max_speech_sec=20.0, pre_roll_sec=0.35):
        self.vad = SileroVad()
        self.start_th, self.end_th = start_th, end_th
        self.min_speech = min_speech_sec
        self.min_silence = min_silence_sec
        self.max_speech = max_speech_sec
        self.pre_roll = pre_roll_sec
        self.reset()

    def reset(self):
        self.vad.reset()
        self.active = False
        self.buf = []
        self.pre = []
        self.speech_sec = 0.0
        self.silence_sec = 0.0
        self.total_sec = 0.0

    def push(self, chunk):
        """喂入一个 512 采样块；返回 (状态, 完成的音频或 None)。

        状态: 'idle' | 'start' | 'speaking' | 'done'
        """
        p = self.vad.speech_prob(chunk)
        dur = len(chunk) / SR
        self.total_sec += dur
        if not self.active:
            self.pre.append(chunk)
            keep = int(self.pre_roll * SR / WIN) + 1
            if len(self.pre) > keep:
                self.pre = self.pre[-keep:]
            if p >= self.start_th:
                self.speech_sec += dur
                if self.speech_sec >= self.min_speech:
                    self.active = True
                    self.buf = list(self.pre)
                    self.silence_sec = 0.0
                    return "start", None
            else:
                self.speech_sec = 0.0
            return "idle", None
        # 已开始
        self.buf.append(chunk)
        if p < self.end_th:
            self.silence_sec += dur
            if self.silence_sec >= self.min_silence:
                audio = np.concatenate(self.buf)
                self.reset()
                return "done", audio
        else:
            self.silence_sec = 0.0
        if self.total_sec > self.max_speech:
            audio = np.concatenate(self.buf)
            self.reset()
            return "done", audio
        return "speaking", None

    def speech_run(self, x):
        """对整段音频跑一遍，返回每块的语音概率（便于诊断）。"""
        self.reset()
        probs = []
        for i in range(0, len(x) - WIN + 1, WIN):
            probs.append(self.vad.speech_prob(x[i:i + WIN]))
        return probs


if __name__ == "__main__":
    import glob
    import wave

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

    v = SileroVad()
    print("=== 她的语音（应为高概率） ===")
    for p in sorted(glob.glob(r"D:\DHS\voice\ref16k\*.wav"))[:3]:
        x = read(p)
        pr = []
        for i in range(0, len(x) - WIN + 1, WIN):
            pr.append(v.speech_prob(x[i:i + WIN]))
        pr = np.array(pr)
        print(f"  {p.split(chr(92))[-1]}: 均值 {pr.mean():.3f} >0.5 占比 {(pr>0.5).mean()*100:.0f}%")

    print("=== 刚才录到的环境噪声（应为低概率） ===")
    for p in [r"D:\DHS\voice\out_selftest.wav"]:
        try:
            x = read(p)
            pr = []
            for i in range(0, len(x) - WIN + 1, WIN):
                pr.append(v.speech_prob(x[i:i + WIN]))
            pr = np.array(pr)
            print(f"  {p.split(chr(92))[-1]}: 均值 {pr.mean():.3f} >0.5 占比 {(pr>0.5).mean()*100:.0f}%")
        except Exception as e:
            print("  跳过", e)
