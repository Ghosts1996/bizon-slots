"""Обходит публичную папку Яндекс Диска и печатает все видео:
size<TAB>modified<TAB>path<TAB>href (ссылка на скачивание)."""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
key = sys.argv[1]


def get(url, **q):
    for i in range(6):
        try:
            with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(q)}", timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(10 * (i + 1))
    raise RuntimeError("429")


def walk(path="/"):
    d = get(API, public_key=key, path=path, limit=200)
    for it in d.get("_embedded", {}).get("items", []):
        time.sleep(1)
        if it.get("type") == "dir":
            yield from walk(it["path"])
        else:
            yield it


for f in walk():
    if (f.get("mime_type") or "").startswith("video"):
        time.sleep(2)
        href = get(API + "/download", public_key=key, path=f["path"])["href"]
        print(f"{f.get('size')}\t{f.get('modified')}\t{f['path']}\t{href}")
