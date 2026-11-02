"""Сервис слотов автовебинара для Сейлбота.

Расписание фиксированное (по МСК), слоты задаются в env SLOTS="14,16,19,21".
Слот исчезает строго в момент старта: в 12:00:01 слот 12:00 уже не отдаётся.
"""
import json
import os
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query

MSK = timezone(timedelta(hours=3))  # в Москве нет перехода на летнее время
SLOTS = sorted(int(h) for h in os.getenv("SLOTS", "14,16,19,21").split(",") if h.strip())
MAX_SLOTS = 4

app = FastAPI(title="bizon-slots")


def now_msk() -> datetime:
    return datetime.now(MSK)


def at(day: datetime, hour: int) -> datetime:
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


def slot_payload(s: datetime, label: str) -> dict:
    return {
        "text": f"{label} {s:%H:%M} по МСК",
        "date": f"{s:%d.%m.%Y}",
        "time": f"{s:%H:%M}",
        "ts": int(s.timestamp()),
        "time_utc": s.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


def build(now: datetime) -> dict:
    today = [slot_payload(at(now, h), "Сегодня") for h in SLOTS if at(now, h) > now]
    tmr = now + timedelta(days=1)
    tomorrow = [slot_payload(at(tmr, h), "Завтра") for h in SLOTS]

    out = {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "has_today": int(bool(today)),
        "today_count": len(today),
        "today": today,
        "tomorrow": tomorrow,
    }
    # Плоские поля: в Сейлботе их удобно сохранять в переменные из ответа API
    for i in range(MAX_SLOTS):
        out[f"t{i + 1}"] = today[i]["text"] if i < len(today) else ""
        out[f"m{i + 1}"] = tomorrow[i]["text"] if i < len(tomorrow) else ""
        out[f"mts{i + 1}"] = tomorrow[i]["ts"] if i < len(tomorrow) else 0
    # b1..b4 — ближайшие 4 сеанса (сначала оставшиеся сегодня, потом завтра).
    # Всегда ровно 4 кнопки, без пустых: в боте не нужны условия на количество.
    upcoming = (today + tomorrow)[:MAX_SLOTS]
    for i, s in enumerate(upcoming):
        out[f"b{i + 1}"] = s["text"]
        out[f"bts{i + 1}"] = s["ts"]
    return out


LABEL_RE = re.compile(r"(сегодня|завтра)\D*(\d{1,2}):(\d{2})", re.IGNORECASE)


MSG_OK = ("✅ Готово, вы записаны: {text}.\n\n"
          "За час до начала пришлю напоминание, а за 5 минут — ссылку на эфир прямо сюда.")
MSG_BAD = "Это время уже недоступно 🙈 Выберите другое 👇"


def resolve(text: str, now: datetime) -> dict:
    r = _resolve(text, now)
    r["msg"] = MSG_OK.format(text=r["text"]) if r.get("valid") else MSG_BAD
    for k in ("text", "date", "time", "ts", "time_utc", "seconds_left",
              "remind_60", "delay_60", "delay_7", "delay_5", "delay_gap", "remind_head", "room_url"):
        r.setdefault(k, "" if k in ("text", "date", "time", "time_utc", "room_url", "remind_head") else 0)
    return r


def _resolve(text: str, now: datetime) -> dict:
    m = LABEL_RE.search(text or "")
    if not m:
        return {"valid": 0, "reason": "bad_text"}
    day = now if m.group(1).lower() == "сегодня" else now + timedelta(days=1)
    hour, minute = int(m.group(2)), int(m.group(3))
    if hour not in SLOTS or minute != 0:
        return {"valid": 0, "reason": "unknown_slot"}
    s = at(day, hour)
    if s <= now:
        return {"valid": 0, "reason": "slot_passed"}
    seconds_left = int((s - now).total_seconds())
    room = os.getenv("ROOM_URL", "https://start.bizon365.ru/room/209601/avtoweb")
    return {
        "valid": 1,
        **slot_payload(s, m.group(1).capitalize()),
        "seconds_left": seconds_left,
        # отложенные сообщения: напоминание за час и ссылка за 7 минут
        "remind_60": int(seconds_left > 3600),
        "delay_60": max(seconds_left - 3600, 0),
        "delay_7": max(seconds_left - 420, 0),
        "delay_5": max(seconds_left - 300, 0),
        # пауза между напоминанием и ссылкой: напоминание уходит через delay_60,
        # ссылка — через delay_5 от записи, значит после напоминания ждём разницу
        "delay_gap": max(seconds_left - 300, 0) - max(seconds_left - 3600, 0),
        "remind_head": "Через час встречаемся!" if seconds_left > 3600 else "Совсем скоро начинаем!",
        "room_url": room,
    }


@app.get("/health")
def health():
    return {"ok": 1}


@app.get("/slots")
def slots():
    return build(now_msk())


def resolve_ts(ts: int, now: datetime) -> dict:
    s = datetime.fromtimestamp(int(ts), MSK)
    label = "Сегодня" if s.date() == now.date() else "Завтра"
    return resolve(f"{label} {s:%H:%M}", now) if s.minute == 0 else {"valid": 0, "reason": "unknown_slot"}


@app.get("/resolve")
def resolve_endpoint(text: str = Query(None, description="Текст нажатой кнопки"),
                     ts: int = Query(None, description="Unix-время сеанса из /slots"),
                     client_id: str = Query(None, description="client_id из Сейлбота")):
    now = now_msk()
    r = resolve_ts(ts, now) if ts else resolve(text or "", now)
    if client_id and r.get("valid"):
        save_registration(client_id, int(r["ts"]), int(now.timestamp()))
    return r


# --- Посещаемость: отчёт Бизона по конкретному сеансу -----------------------

BIZON_API = "https://online.bizon365.ru/api/v1"
ROOM_ID = os.getenv("ROOM_ID", "209601:avtoweb")


def bizon_get(path: str, params: dict) -> dict:
    token = os.getenv("BIZON_TOKEN", "")
    url = f"{BIZON_API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-Token": token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def session_viewers(webinar_id: str) -> list:
    viewers, skip = [], 0
    while True:
        d = bizon_get("webinars/reports/getviewers",
                      {"webinarId": webinar_id, "skip": skip, "limit": 1000})
        chunk = d.get("viewers") or []
        viewers += chunk
        skip += len(chunk)
        if not chunk or skip >= int(d.get("total") or 0):
            return viewers


def check_visit(client_id: str, ts: int) -> dict:
    start = datetime.fromtimestamp(int(ts), MSK)
    webinar_id = f"{ROOM_ID}*{start:%Y-%m-%dT%H:%M:00}"
    try:
        lst = bizon_get("webinars/reports/getlist", {"skip": 0, "limit": 50, "type": "AutoWebinars"})
        ready = any(w.get("webinarId") == webinar_id for w in lst.get("list") or [])
        if not ready:
            return {"ready": 0, "visited": 0, "webinar_id": webinar_id}
        needle = f"salebot={client_id}"
        hit = [v for v in session_viewers(webinar_id)
               if needle in json.dumps(v, ensure_ascii=False)
               or str(v.get("salebot") or "") == str(client_id)]
        return {"ready": 1, "visited": int(bool(hit)), "webinar_id": webinar_id}
    except Exception as e:  # сеть/токен — бот получит ready=0 и проверит позже
        return {"ready": 0, "visited": 0, "webinar_id": webinar_id, "error": str(e)[:200]}


@app.get("/visited")
def visited_endpoint(client_id: str = Query(...), ts: int = Query(None)):
    real_ts = claim_step(client_id, "check", int(now_msk().timestamp()))
    if real_ts is None:
        # устаревшая цепочка или повтор — обе ветки (visited 1/0) не сработают
        return {"ready": 0, "visited": -1}
    return check_visit(client_id, real_ts)


# --- Защита от дублей при повторной записи ---------------------------------
#
# Сейлбот не умеет отменять уже поставленные таймеры, поэтому при перезаписи
# старая цепочка (напоминание/ссылка/проверка) всё равно сработает.
# Сервер помнит последнюю запись клиента и в момент срабатывания решает,
# относится ли этот шаг к актуальной записи и не был ли он уже отправлен.

DB_PATH = os.getenv("DB_PATH", "/opt/bizon-slots/data.db")
TOLERANCE = 10 * 60  # таймеры Сейлбота могут опаздывать на пару минут
_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("create table if not exists reg (client_id text primary key, ts integer, reg_at integer)")
    con.execute("create table if not exists sent (client_id text, ts integer, step text, "
                "primary key (client_id, ts, step))")
    return con


def save_registration(client_id: str, ts: int, reg_at: int) -> None:
    with _lock, db() as con:
        con.execute("insert into reg values (?, ?, ?) on conflict(client_id) do update set "
                    "ts = excluded.ts, reg_at = excluded.reg_at", (client_id, ts, reg_at))


def expected_at(step: str, ts: int, reg_at: int) -> int:
    if step == "hour":
        return max(reg_at, ts - 3600)
    if step == "link":
        return max(reg_at, ts - 300)
    if step == "check":
        return max(reg_at, ts - 300) + 95 * 60
    raise ValueError(step)


def claim_step(client_id: str, step: str, now_ts: int):
    """Возвращает ts сеанса, если шаг актуален и ещё не отправлялся, иначе None."""
    with _lock, db() as con:
        row = con.execute("select ts, reg_at from reg where client_id = ?", (client_id,)).fetchone()
        if not row:
            return None
        ts, reg_at = row
        if abs(now_ts - expected_at(step, ts, reg_at)) > TOLERANCE:
            return None
        cur = con.execute("insert or ignore into sent values (?, ?, ?)", (client_id, ts, step))
        return ts if cur.rowcount else None


HOUR_TEXT = ("⏰ {head}\nВ {time} по МСК начинаем семинар. Захватите блокнот с ручкой, "
             "будут практические упражнения.\n\nСсылку на эфир пришлю за 5 минут до старта. "
             "Подтвердите, что будете 👇")
LINK_TEXT = ("🎬 Через 5 минут встречаемся!\nЖмите кнопку — она откроет страницу эфира.\n\n"
             "Можно зайти прямо сейчас и спокойно подготовиться 👇")


@app.get("/due")
def due_endpoint(client_id: str = Query(...), step: str = Query(..., pattern="^(hour|link)$")):
    now = now_msk()
    ts = claim_step(client_id, step, int(now.timestamp()))
    if ts is None:
        return {"ok": 0, "text": ""}
    s = datetime.fromtimestamp(ts, MSK)
    left = ts - int(now.timestamp())
    head = "Через час встречаемся!" if left > 50 * 60 else "Совсем скоро начинаем!"
    text = HOUR_TEXT.format(head=head, time=f"{s:%H:%M}") if step == "hour" else LINK_TEXT
    return {"ok": 1, "text": text, "time": f"{s:%H:%M}"}
