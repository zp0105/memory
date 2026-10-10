"""视频识别预处理：抽帧 + 提取音轨，供"看图 + 听声"理解视频内容。

用法：
  python video_probe.py <视频文件> [帧数]
输出：
  out/video/frames/frame_000.jpg ...   （可逐张看图）
  out/video/audio.wav                  （16k 单声道，可送 ASR 转写）
"""
import os
import subprocess
import sys

OUT = r"D:\DHS\voice\out\video"


def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path):
    exe = ffmpeg_exe()
    print(f"ffmpeg: {exe}")
    r = subprocess.run([exe, "-hide_banner", "-i", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    info = (r.stderr or "")
    for line in info.splitlines():
        if "Duration" in line or "Stream #" in line:
            print("  " + line.strip()[:120])
    return info


def extract_audio(path, out=os.path.join(OUT, "audio.wav")):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    exe = ffmpeg_exe()
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", path,
           "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", out]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  音轨提取失败: {(r.stderr or '')[:200]}")
        return None
    print(f"  音轨 -> {out} ({os.path.getsize(out)/1024:.0f}KB)")
    return out


def extract_frames(path, n=12, outdir=os.path.join(OUT, "frames")):
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(outdir):
        try:
            os.remove(os.path.join(outdir, f))
        except OSError:
            pass
    import cv2
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    print(f"  视频 {total} 帧 @ {fps:.1f}fps")
    if total <= 0:
        total = 1
    idxs = [int(i * (total - 1) / max(n - 1, 1)) for i in range(n)]
    saved = []
    for k, idx in enumerate(idxs):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        # 长边压到 960，控制体积
        h, w = frame.shape[:2]
        scale = min(1.0, 960 / max(h, w))
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        p = os.path.join(outdir, f"frame_{k:03d}.jpg")
        cv2.imwrite(p, frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        saved.append(p)
    cap.release()
    print(f"  抽帧 {len(saved)} 张 -> {outdir}")
    return saved


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: video_probe.py <视频文件> [帧数]")
    path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    if not os.path.exists(path):
        raise SystemExit(f"找不到视频：{path}")
    print(f"=== 探测 {path} ===")
    probe(path)
    extract_audio(path)
    extract_frames(path, n)
    print("\n下一步：逐张看 out/video/frames/*.jpg，音轨送 ASR 转写。")


if __name__ == "__main__":
    main()
