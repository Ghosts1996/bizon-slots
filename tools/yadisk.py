"""Обходит публичную папку Яндекс Диска и печатает ссылку на самое большое видео.
Список всех файлов - в stderr."""
import json
import sys
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
key = sys.argv[1]


def get(url, **q):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(q)}", timeout=30) as r:
        return json.load(r)


def walk(path="/"):
    d = get(API, public_key=key, path=path, limit=200)
    if d.get("type") != "dir":
        yield d
        return
    for it in d.get("_embedded", {}).get("items", []):
        if it.get("type") == "dir":
            yield from walk(it["path"])
        else:
            yield it


files = list(walk())
for f in files:
    print(f.get("size"), f.get("mime_type"), f["path"], file=sys.stderr)
vids = [f for f in files if (f.get("mime_type") or "").startswith("video")
        or f["path"].lower().endswith((".mp4", ".mov", ".mkv", ".webm"))]
if vids:
    v = max(vids, key=lambda f: f.get("size") or 0)
    print(v["path"], file=sys.stderr)
    print(get(API + "/download", public_key=key, path=v["path"])["href"])
