"""Отчёты по автовебинару и сторож. Доставка — Telegram-бот.

Подписка по ссылкам:
  t.me/<бот>?start=report — ежедневный и недельный отчёт
  t.me/<бот>?start=alert  — то же + уведомления о сбоях

Команды в боте: /today, /yesterday, /week, /help, /stop

python -m app.report daily [--dry]   — отчёт за вчера (по понедельникам + неделя)
python -m app.report watchdog        — проверка токена Бизона
python -m app.report poll            — обработать сообщения бота
python -m app.report test            — тестовое сообщение подписчикам
python -m app.report links           — ссылки для подписки
"""
import html
import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage

from app.main import MSK, ROOM_ID, SLOTS, at, bizon_get, db, session_viewers

STATE_DIR = os.getenv("STATE_DIR", "/opt/bizon-slots")
WEBINAR_TITLE = os.getenv("WEBINAR_TITLE", "Как происходит развитие ребёнка")
SEG = [("full", "🟢", "досмотрели до конца"),
       ("left", "🟡", "ушли до предложения"),
       ("early", "🔴", "ушли в первые 15 минут")]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
LINKS = [("📈 Отчёты Бизона", "https://start.bizon365.ru/admin/reports"),
         ("🤖 Сейлбот", "https://salebot.pro/projects")]


# --- Сбор цифр ----------------------------------------------------------------

def registered_for(con, ts: int) -> set:
    """Клиенты, у которых на момент начала сеанса последней записью был именно этот сеанс."""
    last = {}
    for cid, t in con.execute("select client_id, ts from reg_log where reg_at < ? order by reg_at", (ts,)):
        last[cid] = t
    return {cid for cid, t in last.items() if t == ts}


def day_stats(day: datetime, with_room: bool = True) -> dict:
    """Цифры за день по всем сеансам."""
    slots, segs = [], {k: 0 for k, _, _ in SEG}
    minutes = []
    with db() as con:
        for h in SLOTS:
            start = at(day, h)
            ts = int(start.timestamp())
            regs = registered_for(con, ts)
            rows = con.execute("select client_id, segment, minutes from visits "
                               "where ts = ? and visited = 1", (ts,)).fetchall()
            came = {c for c, _, _ in rows if c in regs}
            for c, sg, mins in rows:
                if c in regs:
                    if sg in segs:
                        segs[sg] += 1
                    minutes.append(mins or 0)
            room = 0
            if with_room and start < datetime.now(MSK):
                try:
                    room = len(session_viewers(f"{ROOM_ID}*{start:%Y-%m-%dT%H:%M:00}"))
                except Exception:
                    room = 0
            slots.append({"hour": h, "reg": len(regs), "came": len(came), "room": room,
                          "passed": start + timedelta(minutes=75) < datetime.now(MSK)})
    reg = sum(s["reg"] for s in slots)
    came = sum(s["came"] for s in slots)
    return {"day": day, "slots": slots, "reg": reg, "came": came,
            "room": sum(s["room"] for s in slots), "segs": segs,
            "avg_min": round(sum(minutes) / len(minutes)) if minutes else 0,
            "pct": round(100 * came / reg) if reg else 0}


# --- Оформление ---------------------------------------------------------------

def bar(part: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = round(width * part / total)
    return "▰" * filled + "▱" * (width - filled)


def delta(now: int, before: int, suffix: str = "") -> str:
    d = now - before
    if d > 0:
        return f" <i>(↑ +{d}{suffix})</i>"
    if d < 0:
        return f" <i>(↓ {d}{suffix})</i>"
    return " <i>(=)</i>"


def human_day(d: datetime) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def advice(st: dict) -> str:
    """Короткий вывод по цифрам — чтобы не нужно было разбираться самому."""
    if st["reg"] == 0:
        return "Записей не было. Проверьте, идёт ли трафик в бота."
    tips = []
    if st["pct"] < 30:
        tips.append("доходимость низкая — стоит усилить напоминания или прогрев перед эфиром")
    elif st["pct"] >= 50:
        tips.append("отличная доходимость")
    if st["came"] and st["segs"]["full"] / st["came"] >= 0.5:
        tips.append("больше половины досматривают до конца — контент держит")
    elif st["came"] and st["segs"]["early"] / st["came"] >= 0.4:
        tips.append("много уходят в начале — посмотрите первые 15 минут записи")
    top = max(st["slots"], key=lambda s: s["reg"])
    if top["reg"]:
        tips.append(f"самое популярное время — {top['hour']:02d}:00")
    if not tips:
        return ""
    line = "; ".join(tips)
    return "💡 " + line[0].upper() + line[1:] + "."


def render_day(st: dict, prev: dict | None, title: str) -> str:
    e = html.escape
    L = [f"<b>{title}</b>", f"<i>{e(WEBINAR_TITLE)}</i>", ""]
    L.append(f"📝 Записались: <b>{st['reg']}</b>" + (delta(st["reg"], prev["reg"]) if prev else ""))
    L.append(f"👀 Пришли: <b>{st['came']}</b>" + (delta(st["came"], prev["came"]) if prev else ""))
    L.append(f"🎯 Доходимость: <b>{st['pct']}%</b>  {bar(st['came'], st['reg'])}"
             + (delta(st["pct"], prev["pct"], "%") if prev and prev["reg"] else ""))
    if st["came"]:
        L += ["", "<b>Как смотрели</b>"]
        for k, icon, name in SEG:
            n = st["segs"][k]
            L.append(f"{icon} {name}: <b>{n}</b>  {bar(n, st['came'], 8)}")
        L.append(f"⏱ В среднем в комнате: <b>{st['avg_min']} мин</b>")
    L += ["", "<b>По сеансам</b>"]
    L.append("<code>время  запис. пришли  %")
    for s in st["slots"]:
        p = f"{round(100 * s['came'] / s['reg'])}%" if s["reg"] else "—"
        L.append(f"{s['hour']:02d}:00  {s['reg']:>5}  {s['came']:>6}  {p:>4}")
    L[-1] += "</code>"
    others = st["room"] - st["came"]
    if others > 0:
        L.append(f"Ещё в комнате без бота: {others} (зашли по прямой ссылке)")
    tip = advice(st)
    if tip:
        L += ["", tip]
    return "\n".join(L)


def render_week(end: datetime) -> str:
    days = [day_stats(end - timedelta(days=i), with_room=False) for i in range(6, -1, -1)]
    reg, came = sum(d["reg"] for d in days), sum(d["came"] for d in days)
    best = max(days, key=lambda d: d["came"])
    by_hour = {h: [0, 0] for h in SLOTS}
    for d in days:
        for s in d["slots"]:
            by_hour[s["hour"]][0] += s["reg"]
            by_hour[s["hour"]][1] += s["came"]
    top_reg = max((d["reg"] for d in days), default=0) or 1
    L = [f"<b>📅 Итоги недели</b>",
         f"<i>{human_day(days[0]['day'])} — {human_day(days[-1]['day'])}</i>", "",
         f"📝 Записались: <b>{reg}</b>",
         f"👀 Пришли: <b>{came}</b>",
         f"🎯 Доходимость: <b>{round(100 * came / reg) if reg else 0}%</b>", "",
         "<b>По дням</b> (записались / пришли)"]
    for d in days:
        L.append(f"{WEEKDAYS[d['day'].weekday()]} {d['day']:%d.%m}  {bar(d['reg'], top_reg, 8)}  "
                 f"{d['reg']} / {d['came']}")
    L += ["", "<b>По времени</b>"]
    for h, (r, c) in by_hour.items():
        L.append(f"{h:02d}:00 — записались {r}, пришли {c}"
                 + (f" ({round(100 * c / r)}%)" if r else ""))
    if best["came"]:
        L += ["", f"🏆 Лучший день: {human_day(best['day'])} — пришли {best['came']}"]
    return "\n".join(L)


def plain(text: str) -> str:
    """HTML → обычный текст для почты."""
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return html.unescape(text)


# --- Доставка -----------------------------------------------------------------

def tg(method: str, **params) -> dict:
    token = os.getenv("TG_TOKEN", "")
    if not token:
        raise RuntimeError("TG_TOKEN не задан")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def tg_send(chat_id: str, text: str, buttons: bool = True) -> None:
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if buttons:
        params["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": t, "url": u} for t, u in LINKS]]})
    tg("sendMessage", **params)


def send_mail(to: str, subject: str, body: str) -> None:
    user, pwd = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    host = os.getenv("SMTP_HOST", "smtp.yandex.ru")
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465")), timeout=30) as smtp:
        smtp.login(user, pwd)
        smtp.send_message(msg)


def subs_table(con):
    con.execute("create table if not exists tg_subs (chat_id text, role text, name text, "
                "primary key (chat_id, role))")
    con.execute("create table if not exists kv (k text primary key, v text)")


def recipients(role: str) -> list:
    with db() as con:
        subs_table(con)
        roles = ("report", "alert") if role == "report" else ("alert",)
        q = "select distinct chat_id from tg_subs where role in (%s)" % ",".join("?" * len(roles))
        return [r[0] for r in con.execute(q, roles)]


def notify(role: str, subject: str, body_html: str) -> int:
    """role: report — всем подписчикам; alert — только подписанным на тревоги."""
    sent = 0
    if os.getenv("TG_TOKEN"):
        try:
            poll()
        except Exception:
            pass
        for cid in recipients(role):
            try:
                tg_send(cid, body_html, buttons=(role == "report"))
                sent += 1
            except Exception:
                pass
    mail_to = os.getenv("REPORT_TO" if role == "report" else "ALERT_TO", "")
    if os.getenv("SMTP_USER") and mail_to:
        send_mail(mail_to, subject, plain(body_html))
        sent += 1
    return sent


# --- Бот: подписка и команды --------------------------------------------------

HELP = ("<b>Бот отчётов автовебинара</b>\n\n"
        "Каждое утро в 9:00 я присылаю итоги за вчера, по понедельникам — итоги недели.\n\n"
        "/today — цифры за сегодня (на текущий момент)\n"
        "/yesterday — итоги за вчера\n"
        "/week — итоги за 7 дней\n"
        "/stop — отписаться")


def handle_command(cid: str, text: str) -> None:
    now = datetime.now(MSK)
    if text.startswith("/today"):
        tg_send(cid, render_day(day_stats(now), None, f"📊 Сегодня, {human_day(now)} (на {now:%H:%M})"))
    elif text.startswith("/yesterday"):
        y = now - timedelta(days=1)
        tg_send(cid, render_day(day_stats(y), day_stats(y - timedelta(days=1), False),
                                f"📊 Итоги за {human_day(y)}"))
    elif text.startswith("/week"):
        tg_send(cid, render_week(now - timedelta(days=1)))
    elif text.startswith("/help"):
        tg_send(cid, HELP, buttons=False)


def poll() -> None:
    """Забирает новые сообщения бота: подписки по /start report|alert и команды."""
    with db() as con:
        subs_table(con)
        row = con.execute("select v from kv where k = 'tg_offset'").fetchone()
        offset = int(row[0]) if row else 0
        if not con.execute("select 1 from kv where k = 'tg_cmds'").fetchone():
            tg("setMyCommands", commands=json.dumps([
                {"command": "today", "description": "Цифры за сегодня"},
                {"command": "yesterday", "description": "Итоги за вчера"},
                {"command": "week", "description": "Итоги за неделю"},
                {"command": "help", "description": "Что умеет бот"},
                {"command": "stop", "description": "Отписаться"}]))
            con.execute("insert or replace into kv values ('tg_cmds', '1')")
        updates = tg("getUpdates", offset=offset, timeout=0).get("result", [])
        subs = {r[0] for r in con.execute("select chat_id from tg_subs")}
        todo = []
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            m = u.get("message") or {}
            text, chat = (m.get("text") or "").strip(), m.get("chat") or {}
            if not chat:
                continue
            cid, name = str(chat["id"]), chat.get("first_name") or chat.get("title") or ""
            parts = text.split()
            role = parts[1] if text.startswith("/start") and len(parts) > 1 else ""
            if role in ("report", "alert"):
                con.execute("insert or replace into tg_subs values (?, ?, ?)", (cid, role, name))
                subs.add(cid)
                todo.append((cid, "welcome", role))
            elif text.startswith("/stop"):
                con.execute("delete from tg_subs where chat_id = ?", (cid,))
                subs.discard(cid)
                todo.append((cid, "bye", ""))
            elif cid in subs and text.startswith("/"):
                todo.append((cid, "cmd", text))
            elif text.startswith("/start"):
                todo.append((cid, "stranger", ""))
        con.execute("insert or replace into kv values ('tg_offset', ?)", (str(offset),))
    for cid, kind, arg in todo:  # отвечаем после записи offset — не будет повторов
        try:
            if kind == "welcome":
                extra = "\n\n🔔 Также буду сообщать о сбоях." if arg == "alert" else ""
                tg_send(cid, "✅ <b>Вы подписаны на отчёты автовебинара.</b>" + extra + "\n\n" + HELP,
                        buttons=False)
            elif kind == "bye":
                tg_send(cid, "Вы отписались. Чтобы вернуться — откройте ссылку подписки ещё раз.",
                        buttons=False)
            elif kind == "stranger":
                tg_send(cid, "Это служебный бот отчётов. Для подписки используйте ссылку, "
                             "которую вам прислали.", buttons=False)
            else:
                handle_command(cid, arg)
        except Exception:
            pass


# --- Задачи по расписанию ------------------------------------------------------

def daily(dry: bool) -> None:
    y = datetime.now(MSK) - timedelta(days=1)
    st, prev = day_stats(y), day_stats(y - timedelta(days=1), with_room=False)
    text = render_day(st, prev, f"📊 Итоги за {human_day(y)}")
    week = render_week(y) if datetime.now(MSK).weekday() == 0 else ""
    if dry:
        print(plain(text))
        if week:
            print("\n" + plain(week))
        return
    notify("report", f"Автовебинар: итоги за {y:%d.%m}", text)
    if week:
        notify("report", "Автовебинар: итоги недели", week)


def watchdog() -> None:
    mark = os.path.join(STATE_DIR, ".alert_sent")
    problem = ""
    try:
        d = bizon_get("webinars/reports/getlist", {"skip": 0, "limit": 1})
        if "list" not in d:
            problem = f"Бизон не принимает API-токен: {d.get('message') or d}"
    except Exception as e:
        problem = f"Нет связи с API Бизона: {e}"
    if not problem:
        if os.path.exists(mark):
            os.remove(mark)
            notify("alert", "Связь с Бизоном восстановлена",
                   "✅ <b>Связь с Бизоном восстановлена</b>\nПроверка посещаемости снова работает.")
        return
    if os.path.exists(mark) and time.time() - os.path.getmtime(mark) < 6 * 3600:
        return  # не спамим: одно уведомление раз в 6 часов
    notify("alert", "Проблема с Бизоном",
           "⚠️ <b>Проблема с Бизоном</b>\n" + html.escape(problem) +
           "\n\nЕсли токен меняли — обновите секрет BIZON_TOKEN в GitHub и перезапустите деплой.")
    open(mark, "w").close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "daily":
        daily("--dry" in sys.argv)
    elif cmd == "watchdog":
        watchdog()
    elif cmd == "poll":
        if os.getenv("TG_TOKEN"):
            poll()
    elif cmd == "test":
        print("sent=%d" % notify("report", "Проверка связи",
                                 "🔔 <b>Проверка связи</b>\nОтчёты автовебинара будут приходить сюда."))
    elif cmd == "links":
        me = tg("getMe")["result"]["username"]
        print(f"https://t.me/{me}?start=report https://t.me/{me}?start=alert")
    else:
        print(__doc__)
