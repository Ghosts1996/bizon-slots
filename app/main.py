"""Сервис слотов автовебинара для Сейлбота.

Расписание фиксированное (по МСК), слоты задаются в env SLOTS="14,16,19,21".
Слот исчезает строго в момент старта: в 12:00:01 слот 12:00 уже не отдаётся.
"""
import os
import re
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
              "remind_60", "delay_60", "delay_7", "delay_5", "room_url"):
        r.setdefault(k, "" if k in ("text", "date", "time", "time_utc", "room_url") else 0)
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
                     ts: int = Query(None, description="Unix-время сеанса из /slots")):
    now = now_msk()
    if ts:
        return resolve_ts(ts, now)
    return resolve(text or "", now)
