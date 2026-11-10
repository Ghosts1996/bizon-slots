"""Закрывает обложкой кадры синей заставки (с датой эфира) в видео.

    python bluefix.py in.mp4 cover.jpg out.mp4

Ищет по видео кадры, где синий цвет занимает почти весь экран (заставка), и на этих
отрезках показывает вместо неё обложку. Звук и длительность не меняются, значит
тайминги сценария Бизона остаются прежними. Найденные отрезки печатаются.
"""
import json
import subprocess
import sys

import numpy as np

SRC, COVER, DST = sys.argv[1:4]
SCAN_FPS = 10
W, H = 64, 36
THRESH = 0.08      # доля синих пикселей; в живом эфире она не выше 0.02
PAD = 0.15         # запас по краям, сек (затухание заставки)
MERGE = 1.0        # соседние отрезки ближе этого сливаем


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,r_frame_rate,duration:format=duration",
                          "-of", "json", path], capture_output=True, check=True, text=True).stdout
    j = json.loads(out)
    n, d = j["streams"][0]["r_frame_rate"].split("/")
    return (int(j["streams"][0]["width"]), int(j["streams"][0]["height"]),
            int(n) / int(d), float(j["format"]["duration"]))


def blue_ratio(path):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={SCAN_FPS},scale={W}:{H}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    size, vals = W * H * 3, []
    while True:
        b = p.stdout.read(size)
        if len(b) < size:
            break
        a = np.frombuffer(b, np.uint8).reshape(H, W, 3).astype(int)
        vals.append(float(((a[..., 2] > 150) & (a[..., 0] < 110) & (a[..., 2] - a[..., 0] > 80)).mean()))
    return vals


def ranges(vals, total):
    runs, i = [], 0
    while i < len(vals):
        if vals[i] > THRESH:
            j = i
            while j + 1 < len(vals) and vals[j + 1] > THRESH:
                j += 1
            runs.append([i / SCAN_FPS, (j + 1) / SCAN_FPS])
            i = j + 1
        else:
            i += 1
    merged = []
    for s, e in runs:
        if merged and s - merged[-1][1] < MERGE:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [[max(0.0, s - PAD) if s > PAD * 2 else 0.0, e + PAD if e < total - PAD * 2 else total + 1]
            for s, e in merged]


w, h, fps, dur = probe(SRC)
print(f"video {w}x{h} {fps:.3f}fps {dur:.2f}s", flush=True)
rs = ranges(blue_ratio(SRC), dur)
fmt = lambda s: f"{int(s) // 60}:{s % 60:05.2f}"
for s, e in rs:
    print(f"BLUE {fmt(s)} - {fmt(min(e, dur))}  ({min(e, dur) - s:.1f}s)", flush=True)
if not rs:
    sys.exit("заставка не найдена, ничего не меняю")

enable = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in rs)
flt = (f"[1:v]scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p[c];"
       f"[0:v]setsar=1[v];[v][c]overlay=0:0:shortest=1:enable='{enable}'[o]")
cmd = ["ffmpeg", "-v", "error", "-stats", "-y", "-i", SRC, "-loop", "1", "-framerate", f"{fps}",
       "-t", f"{dur + 2:.2f}", "-i", COVER, "-filter_complex", flt, "-map", "[o]", "-map", "0:a?",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
       "-c:a", "copy", "-movflags", "+faststart", "-fps_mode", "passthrough", DST]
subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
w2, h2, fps2, dur2 = probe(DST)
print(f"out {w2}x{h2} {fps2:.3f}fps {dur2:.2f}s (было {dur:.2f}s)", flush=True)
json.dump({"ranges": rs, "dur_in": dur, "dur_out": dur2}, open(DST + ".json", "w"))
