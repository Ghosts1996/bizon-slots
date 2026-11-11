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
    # b1..b4 - ближайшие 4 сеанса (сначала оставшиеся сегодня, потом завтра).
    # Всегда ровно 4 кнопки, без пустых: в боте не нужны условия на количество.
    upcoming = (today + tomorrow)[:MAX_SLOTS]
    for i, s in enumerate(upcoming):
        out[f"b{i + 1}"] = s["text"]
        out[f"bts{i + 1}"] = s["ts"]
    return out


LABEL_RE = re.compile(r"(сегодня|завтра)\D*(\d{1,2}):(\d{2})", re.IGNORECASE)


MSG_OK = ("✅ Готово, вы записаны!\n\n"
          "📅 {day} {date} в {time} по МСК.\n\n"
          "Чтобы ничего не пропустить, добавьте эфир в календарь и включите уведомления от этого чата. "
          "За час до начала пришлю напоминание, а за 5 минут ссылку на эфир.\n\n"
          "Если планы изменятся, время можно поменять кнопкой ниже 👇")
CAL_TITLE = os.getenv("CAL_TITLE", "Эфир Натальи Чернецкой: 3 главных звоночка, что у ребёнка проблемы с чтением")
CAL_MIN = int(os.getenv("CAL_MIN", "90"))


def calendar_url(s: datetime) -> str:
    """Ссылка добавить в Google Календарь - открывается и на телефоне, без сервера."""
    fmt = "%Y%m%dT%H%M%SZ"
    start = s.astimezone(timezone.utc)
    end = start + timedelta(minutes=CAL_MIN)
    q = urllib.parse.urlencode({
        "action": "TEMPLATE", "text": CAL_TITLE,
        "dates": f"{start:{fmt}}/{end:{fmt}}",
        "details": "Ссылку на эфир пришлём в чат-бот за 5 минут до начала.",
        "ctz": "Europe/Moscow"})
    return f"https://calendar.google.com/calendar/render?{q}"
MSG_BAD = "Это время уже недоступно 🙈 Выберите другое 👇"


def resolve(text: str, now: datetime) -> dict:
    r = _resolve(text, now)
    if r.get("valid"):
        r["msg"] = MSG_OK.format(day=r["text"].split()[0], date=r["date"], time=r["time"])
        r["cal_url"] = calendar_url(datetime.fromtimestamp(r["ts"], MSK))
    else:
        r["msg"] = MSG_BAD
    for k in ("cal_url", "text", "date", "time", "ts", "time_utc", "seconds_left",
              "remind_60", "delay_60", "delay_7", "delay_5", "delay_gap", "remind_head", "room_url"):
        r.setdefault(k, "" if k in ("cal_url", "text", "date", "time", "time_utc", "room_url", "remind_head") else 0)
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
        # ссылка - через delay_5 от записи, значит после напоминания ждём разницу
        "delay_gap": max(seconds_left - 300, 0) - max(seconds_left - 3600, 0),
        "remind_head": "Через час встречаемся!" if seconds_left > 3600 else "Совсем скоро начинаем!",
        "room_url": room,
    }


# Готовые файлы для передачи клиенту (видео и т.п.), по ссылке с ключом
FILES_DIR = os.getenv("FILES_DIR", "/opt/video/out")
FILES_KEY = os.getenv("FILES_KEY", "9efOx-bXPs1Dm_IC")


@app.get("/files/{key}/{name}")
def files(key: str, name: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    path = os.path.join(FILES_DIR, os.path.basename(name))
    if key != FILES_KEY or not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, filename=os.path.basename(name))


@app.get("/health")
def health():
    return {"ok": 1}


@app.get("/slots")
def slots(client_id: str = Query(None, description="client_id из Сейлбота: отметка приветствия для дожима")):
    now = now_msk()
    if client_id:
        save_start(client_id, int(now.timestamp()))
    return build(now)


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
        r["room_link"] = room_link(client_id)  # room_url не трогаем: в блоках Сейлбота к нему уже могут дописывать ?salebot=
    return r


# --- Посещаемость: отчёт Бизона по конкретному сеансу -----------------------

BIZON_API = "https://online.bizon365.ru/api/v1"
ROOM_ID = os.getenv("ROOM_ID", "209601:avtoweb")
EMPTY_AFTER_MIN = int(os.getenv("EMPTY_AFTER_MIN", "80"))


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


def is_tagged(v: dict) -> bool:
    return "salebot=" in json.dumps(v, ensure_ascii=False) or bool(v.get("salebot"))


def room_link(client_id: str = "") -> str:
    room = os.getenv("ROOM_URL", "https://start.bizon365.ru/room/209601/avtoweb")
    return f"{room}?salebot={urllib.parse.quote(str(client_id))}" if client_id else room


def check_visit(client_id: str, ts: int) -> dict:
    start = datetime.fromtimestamp(int(ts), MSK)
    webinar_id = f"{ROOM_ID}*{start:%Y-%m-%dT%H:%M:00}"
    try:
        lst = bizon_get("webinars/reports/getlist", {"skip": 0, "limit": 50, "type": "AutoWebinars"})
        ready = any(w.get("webinarId") == webinar_id for w in lst.get("list") or [])
        viewers = session_viewers(webinar_id)
        # Бизон не заводит отчёт по сеансу, в который никто не зашёл. Если эфир
        # давно закончился, а отчёта нет - значит, в комнате не было никого.
        if not ready and not viewers and now_msk() < start + timedelta(minutes=EMPTY_AFTER_MIN):
            return {**NO_FOLLOWUP, "ready": 0, "visited": 0, "webinar_id": webinar_id}
        needle = f"salebot={client_id}"
        hit = [v for v in viewers
               if needle in json.dumps(v, ensure_ascii=False)
               or str(v.get("salebot") or "") == str(client_id)]
        minutes = round(sum(watched_minutes(v) for v in hit), 1)
        seg = segment(bool(hit), minutes)
        # В комнате были люди без метки salebot= (зашли не по ссылке из бота и
        # ввели имя вручную) - среди них может быть и наш клиент. Не пишем ему
        # «мы вас не видели», пока не можем это утверждать.
        if not hit and any(not is_tagged(v) for v in viewers):
            return {**NO_FOLLOWUP, "ready": 1, "visited": 0, "unsure": 1,
                    "webinar_id": webinar_id, "minutes": 0, "segment": "none",
                    "seg_early": 0, "seg_left": 0, "seg_full": 0}
        save_visit(client_id, int(ts), int(bool(hit)), minutes, seg)
        return {**NO_FOLLOWUP, **followup(client_id, bool(hit)),
                "ready": 1, "visited": int(bool(hit)), "webinar_id": webinar_id,
                "minutes": minutes, "segment": seg,
                "seg_early": int(seg == "early"), "seg_left": int(seg == "left"),
                "seg_full": int(seg == "full"), "unsure": 0}
    except Exception as e:  # сеть/токен - бот получит ready=0 и проверит позже
        return {**NO_FOLLOWUP, "ready": 0, "visited": 0, "webinar_id": webinar_id,
                "error": str(e)[:200]}


# Догон для тех, кто не пришёл:
#   1-й пропуск - сразу после эфира снова предлагаем выбрать время (again=1);
#   2-й пропуск подряд - на следующее утро отправляем запись (record=1).
# Флаги ставятся только когда отчёт Бизона готов, поэтому ложных не пришёл нет.
RECORD_HOUR = int(os.getenv("RECORD_HOUR", "10"))
NO_FOLLOWUP = {"miss": 0, "again": 0, "record": 0, "delay_record": 0}


def misses_in_row(client_id: str) -> int:
    with _lock, db() as con:
        last_seen = con.execute("select coalesce(max(ts), 0) from visits "
                                "where client_id = ? and visited = 1", (client_id,)).fetchone()[0]
        return con.execute("select count(*) from visits where client_id = ? and visited = 0 "
                           "and ts > ?", (client_id, last_seen)).fetchone()[0]


def record_at(check_ts: int) -> int:
    day = datetime.fromtimestamp(check_ts, MSK) + timedelta(days=1)
    return int(at(day, RECORD_HOUR).timestamp())


def followup(client_id: str, visited: bool) -> dict:
    if visited:
        return {}
    miss = misses_in_row(client_id)
    now_ts = int(now_msk().timestamp())
    return {"miss": miss, "again": int(miss == 1), "record": int(miss >= 2),
            "delay_record": max(record_at(now_ts) - now_ts, 0)}


@app.get("/visited")
def visited_endpoint(client_id: str = Query(...), ts: int = Query(None)):
    real_ts = claim_step(client_id, "check", int(now_msk().timestamp()))
    if real_ts is None:
        # устаревшая цепочка или повтор - обе ветки (visited 1/0) не сработают
        return {**NO_FOLLOWUP, "ready": 0, "visited": -1, "minutes": 0, "segment": "",
                "seg_early": 0, "seg_left": 0, "seg_full": 0}
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
    # история всех записей (reg хранит только последнюю) - для отчётов
    con.execute("create table if not exists reg_log (client_id text, ts integer, reg_at integer)")
    con.execute("create table if not exists visits (client_id text, ts integer, visited integer, "
                "minutes real, segment text, primary key (client_id, ts))")
    con.execute("create table if not exists starts (client_id text primary key, started_at integer)")
    return con


def save_registration(client_id: str, ts: int, reg_at: int) -> None:
    with _lock, db() as con:
        con.execute("insert into reg values (?, ?, ?) on conflict(client_id) do update set "
                    "ts = excluded.ts, reg_at = excluded.reg_at", (client_id, ts, reg_at))
        con.execute("insert into reg_log values (?, ?, ?)", (client_id, ts, reg_at))


def expected_at(step: str, ts: int, reg_at: int) -> int:
    if step == "hour":
        return max(reg_at, ts - 3600)
    if step == "link":
        return max(reg_at, ts - 300)
    if step == "check":
        return max(reg_at, ts - 300) + 95 * 60
    if step == "record":
        return record_at(expected_at("check", ts, reg_at))
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
LINK_TEXT = ("🎬 Через 5 минут встречаемся!\nЖмите кнопку, она откроет страницу эфира.\n\n"
             "Можно зайти прямо сейчас и спокойно подготовиться 👇")


@app.get("/due")
def due_endpoint(client_id: str = Query(...), step: str = Query(..., pattern="^(hour|link|record)$")):
    now = now_msk()
    ts = claim_step(client_id, step, int(now.timestamp()))
    if ts is None:
        return {"ok": 0, "text": "", "url": ""}
    if step == "record":
        # текст и ссылка на запись живут в блоке Сейлбота, чтобы их можно было править без меня
        return {"ok": 1, "text": ""}
    s = datetime.fromtimestamp(ts, MSK)
    left = ts - int(now.timestamp())
    head = "Через час встречаемся!" if left > 50 * 60 else "Совсем скоро начинаем!"
    text = HOUR_TEXT.format(head=head, time=f"{s:%H:%M}") if step == "hour" else LINK_TEXT
    return {"ok": 1, "text": text, "time": f"{s:%H:%M}", "url": room_link(client_id)}

# --- Дожим тех, кто запустил бота, но не выбрал время ------------------------
#
# Сейлбот после приветствия вызывает /started и ставит таймер на delay_nudge
# секунд (по умолчанию 30 минут). Когда таймер сработал, бот вызывает /nudge:
# ok=1 - человек так и не записался, шлём напоминание с актуальными кнопками;
# ok=0 - уже выбрал время (или повтор/устаревший таймер), ничего не шлём.
# Тот же /nudge с step=day защищает дожим «завтра» от отправки записавшимся.

NUDGE_MIN = int(os.getenv("NUDGE_MIN", "30"))
NUDGE_TEXT = ("Видимо, вас что-то отвлекло 😊\n"
              "Осталось выбрать удобное время эфира - участие бесплатное 👇")


def save_start(client_id: str, started_at: int) -> None:
    with _lock, db() as con:
        con.execute("insert into starts values (?, ?) on conflict(client_id) do update set "
                    "started_at = excluded.started_at", (client_id, started_at))


# Дожимы после /start: шаг -> через сколько минут после приветствия.
NUDGE_STEPS = {"30m": NUDGE_MIN, "1h": 60, "2h": 120}
NUDGE_EARLY = 120          # таймер Сейлбота может сработать чуть раньше/позже
DAY_AFTER_START = 150      # «завтра»-дожим не мешает цепочке 30м/1ч/2ч


def claim_nudge(client_id: str, step: str, now_ts: int) -> bool:
    """True - дожим этого шага надо отправить (и он помечен отправленным)."""
    with _lock, db() as con:
        row = con.execute("select started_at from starts where client_id = ?", (client_id,)).fetchone()
        started_at = row[0] if row else 0
        if started_at:
            age = now_ts - started_at
            if step in NUDGE_STEPS and age < NUDGE_STEPS[step] * 60 - NUDGE_EARLY:
                return False  # таймер от прошлого /start, а человек нажал /start ещё раз
            if step == "day" and age < DAY_AFTER_START * 60:
                return False  # идёт цепочка дожимов после /start, лишний не шлём
        # выбрал время после приветствия - не дожимаем
        if con.execute("select 1 from reg_log where client_id = ? and reg_at >= ? limit 1",
                       (client_id, started_at)).fetchone():
            return False
        # уже записан на будущий сеанс (записался раньше и снова нажал /start) - тоже не дожимаем
        if con.execute("select 1 from reg where client_id = ? and ts > ?", (client_id, now_ts)).fetchone():
            return False
        cur = con.execute("insert or ignore into sent values (?, ?, ?)",
                          (client_id, started_at, f"nudge_{step}"))
        return bool(cur.rowcount)


@app.get("/started")
def started_endpoint(client_id: str = Query(...)):
    save_start(client_id, int(now_msk().timestamp()))
    return {"ok": 1, "delay_nudge": NUDGE_MIN * 60}


@app.get("/nudge")
def nudge_endpoint(client_id: str = Query(...), step: str = Query("30m", pattern="^(30m|1h|2h|day)$")):
    now = now_msk()
    if not claim_nudge(client_id, step, int(now.timestamp())):
        return {"ok": 0, "text": "", **{f"b{i}": "" for i in range(1, MAX_SLOTS + 1)}}
    s = build(now)
    return {"ok": 1, "text": NUDGE_TEXT, **{f"b{i}": s[f"b{i}"] for i in range(1, MAX_SLOTS + 1)},
            **{f"bts{i}": s[f"bts{i}"] for i in range(1, MAX_SLOTS + 1)}}


# --- Сегменты по времени просмотра ------------------------------------------

EARLY_MIN = float(os.getenv("EARLY_MIN", "15"))   # ушёл в начале
OFFER_MIN = float(os.getenv("OFFER_MIN", "46"))   # минута, на которой начинается предложение (баннер записи 46:15)


def watched_minutes(v: dict) -> float:
    """Сколько минут зритель был в комнате. vi - интервалы просмотра в секундах."""
    vi = v.get("vi") or []
    if isinstance(vi, list) and vi:
        return sum(max(0, (i.get("e") or 0) - (i.get("s") or 0)) for i in vi) / 60
    try:
        return max(0, int(v.get("viewTill")) - int(v.get("view"))) / 60000
    except (TypeError, ValueError):
        return 0.0


def segment(visited: bool, minutes: float) -> str:
    if not visited:
        return "none"
    if minutes < EARLY_MIN:
        return "early"
    if minutes < OFFER_MIN:
        return "left"
    return "full"


def save_visit(client_id: str, ts: int, visited: int, minutes: float, seg: str) -> None:
    with _lock, db() as con:
        con.execute("insert or replace into visits values (?, ?, ?, ?, ?)",
                    (client_id, ts, visited, minutes, seg))
