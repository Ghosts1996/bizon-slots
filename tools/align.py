"""Сопоставляет новое видео со старым по звуку: где новое совпадает со старым
и на сколько сдвинуто. Печатает строки: new_sec old_sec score."""
import subprocess
import sys

import numpy as np

HOP = 0.05  # шаг огибающей, сек


def env(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32)
    n = int(8000 * HOP)
    x = x[: len(x) // n * n].reshape(-1, n)
    e = np.log1p(np.sqrt((x ** 2).mean(1)))
    return e - e.mean()


def best(chunk, ref):
    c = (chunk - chunk.mean()) / (chunk.std() + 1e-9)
    m = len(c)
    corr = np.correlate(ref, c, mode="valid")
    cs = np.concatenate([[0], np.cumsum(ref)]); cs2 = np.concatenate([[0], np.cumsum(ref ** 2)])
    mu = (cs[m:] - cs[:-m]) / m
    sd = np.sqrt(np.maximum((cs2[m:] - cs2[:-m]) / m - mu ** 2, 1e-9))
    score = corr / (sd * m)
    score[sd < 0.3 * ref.std()] = -1  # тишина/заставка - не сравниваем
    i = int(score.argmax())
    return i, float(score[i])


new, old = env(sys.argv[1]), env(sys.argv[2])
print(f"dur new={len(new) * HOP:.1f} old={len(old) * HOP:.1f}")
win, step = int(20 / HOP), int(10 / HOP)
for s in range(0, len(new) - win, step):
    i, sc = best(new[s:s + win], old)
    print(f"{s * HOP:8.2f} {i * HOP:8.2f} {i * HOP - s * HOP:8.2f} {sc:.3f}")
