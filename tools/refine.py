"""Точные границы выреза: кусок нового видео [NS, NS+ND] против старого [OS, OS+OD].
Печатает new_sec old_sec offset score с шагом 1 сек (окна по 4 сек)."""
import subprocess, sys
import numpy as np
HOP = 0.02

def env(path, ss, dur):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(ss), "-t", str(dur), "-i", path,
                          "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True,
                         stdin=subprocess.DEVNULL).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32)
    n = int(8000 * HOP); x = x[: len(x) // n * n].reshape(-1, n)
    e = np.log1p(np.sqrt((x ** 2).mean(1))); return e - e.mean()

new_p, ns, nd, old_p, os_, od = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], float(sys.argv[5]), float(sys.argv[6])
new, old = env(new_p, ns, nd), env(old_p, os_, od)
win = int(4 / HOP)
for s in range(0, len(new) - win, int(1 / HOP)):
    c = new[s:s + win]; c = (c - c.mean()) / (c.std() + 1e-9)
    best = (-9, 0)
    for i in range(0, len(old) - win):
        w = old[i:i + win]; sd = w.std()
        if sd < 1e-6: continue
        sc = float(((w - w.mean()) / sd * c).mean())
        if sc > best[0]: best = (sc, i)
    t_new = ns + s * HOP; t_old = os_ + best[1] * HOP
    print(f"{t_new:8.2f} {t_old:8.2f} {t_old - t_new:8.2f} {best[0]:.3f}", flush=True)
