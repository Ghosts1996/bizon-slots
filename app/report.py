"""Отчёты по автовебинару и сторож. Доставка - Telegram-бот.

Доступ - только по приглашению (см. раздел Доступы). Первый вход владельца -
по ссылке из `python -m app.report links`, дальше владелец/админы приглашают
людей кнопкой 👥 Доступы в боте.

python -m app.report daily [--dry]   - отчёт за вчера (по понедельникам + неделя)
python -m app.report watchdog        - проверка токена Бизона
python -m app.report serve           - бот: подписки, меню и кнопки (постоянно)
python -m app.report test            - тестовое сообщение подписчикам
python -m app.report links           - ссылка первого входа владельца
"""
import html
import json
import os
import smtplib
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage

from app.main import MSK, ROOM_ID, SLOTS, at, bizon_get, db, segment, session_viewers, viewer_ids, watched_minutes

STATE_DIR = os.getenv("STATE_DIR", "/opt/bizon-slots")
SEG = [("full", "🟢", "досмотрели до конца"),
       ("left", "🟡", "ушли до предложения"),
       ("early", "🔴", "смотрели меньше 15 минут")]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
LINKS = [("🎥 Открыть Бизон", "https://start.bizon365.ru/"),
         ("🤖 Открыть Сейлбот", "https://salebot.pro/")]


# --- Сбор цифр ----------------------------------------------------------------

def registered_for(con, ts: int) -> set:
    """Клиенты, у которых на момент начала сеанса последней записью был именно этот сеанс."""
    last = {}
    for cid, t in con.execute("select client_id, ts from reg_log where reg_at < ? order by reg_at", (ts,)):
        last[cid] = t
    return {cid for cid, t in last.items() if t == ts}


def sync_visits(con, start: datetime, ts: int, regs: set) -> None:
    """Сверяет записавшихся с отчётом Бизона сразу после эфира - не ждём проверки из бота."""
    known = {r[0] for r in con.execute("select client_id from visits where ts = ?", (ts,))}
    todo = regs - known
    if not todo:
        return
    try:
        viewers = session_viewers(f"{ROOM_ID}*{start:%Y-%m-%dT%H:%M:00}")
    except Exception:
        return
    if not viewers:
        return  # отчёт ещё не готов
    blobs = [(json.dumps(v, ensure_ascii=False), v) for v in viewers]
    for cid in todo:
        hit = [v for b, v in blobs if str(cid) in viewer_ids(v)]
        mins = round(sum(watched_minutes(v) for v in hit), 1)
        con.execute("insert or replace into visits values (?, ?, ?, ?, ?)",
                    (cid, ts, int(bool(hit)), mins, segment(bool(hit), mins)))


_chat_cache: dict = {}


def session_chat(start: datetime) -> list:
    """Все сообщения чата сеанса из отчёта Бизона, по порядку."""
    ts = int(start.timestamp())
    if ts in _chat_cache:
        return _chat_cache[ts]
    try:
        d = bizon_get("webinars/reports/get", {"webinarId": f"{ROOM_ID}*{start:%Y-%m-%dT%H:%M:00}"})
    except Exception:
        return []
    rep = d.get("report") or {}
    out = []
    if isinstance(rep, dict) and rep.get("messages"):
        msgs = _loads(rep.get("messages"))
        stamps = _loads(rep.get("messagesTS"))
        meta = _loads(rep.get("report")).get("usersMeta") or {}
        for uid, texts in msgs.items():
            m = meta.get(uid) or {}
            name = str(m.get("username") or "Гость").strip() or "Гость"
            sec = stamps.get(uid) or []
            for i, t in enumerate(texts if isinstance(texts, list) else [texts]):
                if isinstance(t, dict):
                    t = t.get("text") or t.get("message") or ""
                t = str(t).strip()
                if t:
                    out.append({"sec": int(sec[i]) if i < len(sec) and sec[i] else 0,
                                "name": name, "text": t,
                                "bot": "salebot=" in str(m.get("url") or "")})
        out.sort(key=lambda x: x["sec"])
    if datetime.now(MSK) > start + timedelta(hours=3):  # отчёт уже не изменится
        _chat_cache[ts] = out
    return out


def _loads(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        r = json.loads(v or "{}")
        return r if isinstance(r, dict) else {}
    except ValueError:
        return {}


def day_stats(day: datetime, with_room: bool = True) -> dict:
    """Цифры за день по всем сеансам."""
    slots, segs = [], {k: 0 for k, _, _ in SEG}
    minutes = []
    with db() as con:
        for h in SLOTS:
            start = at(day, h)
            ts = int(start.timestamp())
            regs = registered_for(con, ts)
            if regs and start + timedelta(minutes=80) < datetime.now(MSK):
                sync_visits(con, start, ts, regs)
            rows = con.execute("select client_id, segment, minutes from visits "
                               "where ts = ? and visited = 1", (ts,)).fetchall()
            came = {c for c, _, _ in rows if c in regs}
            for c, sg, mins in rows:
                if c in regs:
                    if sg in segs:
                        segs[sg] += 1
                    minutes.append(mins or 0)
            room, chat = 0, []
            if with_room and start < datetime.now(MSK):
                try:
                    room = len(session_viewers(f"{ROOM_ID}*{start:%Y-%m-%dT%H:%M:00}"))
                except Exception:
                    room = 0
                if room:
                    chat = session_chat(start)
            slots.append({"hour": h, "reg": len(regs), "came": len(came), "room": room,
                          "chat": chat,
                          "passed": start + timedelta(minutes=75) < datetime.now(MSK)})
    reg = sum(s["reg"] for s in slots)
    came = sum(s["came"] for s in slots)
    chat = [dict(m, hour=s["hour"]) for s in slots for m in s["chat"]]
    return {"day": day, "slots": slots, "reg": reg, "came": came, "chat": chat,
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
    """Короткий вывод по цифрам - чтобы не нужно было разбираться самому."""
    if st["reg"] == 0:
        return "Записей не было. Проверьте, идёт ли трафик в бота."
    tips = []
    if st["pct"] < 30:
        tips.append("доходимость низкая - стоит усилить напоминания или прогрев перед эфиром")
    elif st["pct"] >= 50:
        tips.append("отличная доходимость")
    if st["came"] and st["segs"]["full"] / st["came"] >= 0.5:
        tips.append("больше половины досматривают до конца - контент держит")
    elif st["came"] and st["segs"]["early"] / st["came"] >= 0.4:
        tips.append("многие смотрят меньше 15 минут - проверьте начало записи и время входа")
    top = max(st["slots"], key=lambda s: s["reg"])
    if top["reg"]:
        tips.append(f"самое популярное время - {top['hour']:02d}:00")
    if not tips:
        return ""
    line = "; ".join(tips)
    return "💡 " + line[0].upper() + line[1:] + "."


def render_day(st: dict, prev: dict | None, title: str) -> str:
    e = html.escape
    L = [f"<b>{title}</b>", ""]
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
        p = f"{round(100 * s['came'] / s['reg'])}%" if s["reg"] else "-"
        L.append(f"{s['hour']:02d}:00  {s['reg']:>5}  {s['came']:>6}  {p:>4}")
    L[-1] += "</code>"
    others = st["room"] - st["came"]
    if others > 0:
        L.append(f"Ещё в комнате без бота: {others} (зашли по прямой ссылке)")
    if st.get("chat"):
        n, people = len(st["chat"]), len({(m["hour"], m["name"]) for m in st["chat"]})
        L += ["", f"💬 Сообщений в чате: <b>{n}</b> от {people} "
                  f"{plural(people, 'человека', 'человек', 'человек')}"]
    tip = advice(st)
    if tip:
        L += ["", tip]
    return "\n".join(L)


CHAT_PREVIEW = 8


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def chat_line(m: dict, with_hour: bool = True) -> str:
    t = m["text"] if len(m["text"]) <= 300 else m["text"][:300] + "…"
    when = f"{m['sec'] // 60}:{m['sec'] % 60:02d}"
    head = f"{m['hour']:02d}:00 · {when}" if with_hour else when
    return f"<code>{head}</code> <b>{html.escape(m['name'])}</b>: {html.escape(t)}"


def render_chat(day: datetime, st: dict) -> str:
    """Весь чат за день по сеансам. Влезает в одно сообщение Telegram."""
    head = f"<b>💬 Чат эфиров, {human_day(day)}</b>"
    if not st["chat"]:
        return head + "\n\nВ этот день в чате никто не писал."
    blocks = []
    for s in st["slots"]:
        if s["chat"]:
            blocks.append(f"\n<b>{s['hour']:02d}:00</b>, {len(s['chat'])} "
                          f"{plural(len(s['chat']), 'сообщение', 'сообщения', 'сообщений')}")
            blocks += [chat_line(dict(m, hour=s["hour"]), with_hour=False) for m in s["chat"]]
    text, skipped = head + "\n" + "\n".join(blocks), 0
    while len(text) > 3900 and len(blocks) > 1:
        blocks.pop(1 if blocks[0].startswith("\n<b>") else 0)
        skipped += 1
        text = head + f"\n<i>первые {skipped} строк не влезли, показаны последние</i>\n" + "\n".join(blocks)
    note = "\n\n<i>Время рядом с сообщением: минута эфира, на которой его написали.</i>"
    return text + note


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
         f"<i>{human_day(days[0]['day'])} - {human_day(days[-1]['day'])}</i>", "",
         f"📝 Записались: <b>{reg}</b>",
         f"👀 Пришли: <b>{came}</b>",
         f"🎯 Доходимость: <b>{round(100 * came / reg) if reg else 0}%</b>", "",
         "<b>По дням</b> (записались / пришли)"]
    for d in days:
        L.append(f"{WEEKDAYS[d['day'].weekday()]} {d['day']:%d.%m}  {bar(d['reg'], top_reg, 8)}  "
                 f"{d['reg']} / {d['came']}")
    L += ["", "<b>По времени</b>"]
    for h, (r, c) in by_hour.items():
        L.append(f"{h:02d}:00 - записались {r}, пришли {c}"
                 + (f" ({round(100 * c / r)}%)" if r else ""))
    if best["came"]:
        L += ["", f"🏆 Лучший день: {human_day(best['day'])} - пришли {best['came']}"]
    return "\n".join(L)


def plain(text: str) -> str:
    """HTML → обычный текст для почты."""
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return html.unescape(text)


# --- Доставка -----------------------------------------------------------------

# У российских хостингов часть адресов api.telegram.org недоступна. Пробуем
# рабочие IP по очереди (сертификат проверяется по имени, так что это безопасно).
TG_HOST = "api.telegram.org"
TG_IPS = [ip for ip in os.getenv("TG_API_IPS", "149.154.167.220,149.154.167.99,"
                                 "149.154.166.110,149.154.175.100").split(",") if ip]
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo(host, port, *args, **kwargs):
    if host == TG_HOST:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in TG_IPS]
    return _orig_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo

def tg(method: str, _http_timeout: int = 8, **params) -> dict:
    token = os.getenv("TG_TOKEN", "")
    if not token:
        raise RuntimeError("TG_TOKEN не задан")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    last = None
    for _ in range(len(TG_IPS)):
        try:
            with urllib.request.urlopen(req, timeout=_http_timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except OSError as e:  # адрес недоступен - переставляем его в конец и пробуем следующий
            last = e
            print(f"tg {method}: {TG_IPS[0]} недоступен ({e}), пробую следующий", flush=True)
            TG_IPS.append(TG_IPS.pop(0))
    raise last


# Постоянное меню внизу чата - чтобы не вспоминать команды
BTN_TODAY, BTN_YESTERDAY, BTN_WEEK, BTN_HELP = "📊 Сегодня", "🗓 Вчера", "📈 Неделя", "ℹ️ Помощь"
BTN_ACCESS = "👥 Доступы"
MENU = {"keyboard": [[{"text": BTN_TODAY}, {"text": BTN_YESTERDAY}],
                     [{"text": BTN_WEEK}, {"text": BTN_HELP}]],
        "resize_keyboard": True, "is_persistent": True,
        "input_field_placeholder": "Выберите отчёт 👇"}


def links_row() -> list:
    return [{"text": t, "url": u} for t, u in LINKS]


def day_keyboard(day: datetime) -> dict:
    """Под дневным отчётом: листать дни, обновить, перейти к неделе, ссылки."""
    today = datetime.now(MSK).date()
    prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
    nav = [{"text": f"◀️ {prev_d:%d.%m}", "callback_data": f"d:{prev_d:%Y-%m-%d}"}]
    if day.date() == today:
        nav.append({"text": "🔄 Обновить", "callback_data": f"d:{day:%Y-%m-%d}"})
    else:
        nav.append({"text": f"{next_d:%d.%m} ▶️", "callback_data": f"d:{next_d:%Y-%m-%d}"})
    return {"inline_keyboard": [nav,
                                [{"text": "📈 Итоги недели", "callback_data": f"w:{day:%Y-%m-%d}"}],
                                links_row()]}


def chat_keyboard(day: datetime) -> dict:
    return {"inline_keyboard": [[{"text": "📊 Назад к отчёту", "callback_data": f"d:{day:%Y-%m-%d}"}],
                                links_row()]}


def week_keyboard(end: datetime) -> dict:
    prev_e, next_e = end - timedelta(days=7), end + timedelta(days=7)
    nav = [{"text": "◀️ Пред. неделя", "callback_data": f"w:{prev_e:%Y-%m-%d}"}]
    if next_e.date() < datetime.now(MSK).date():
        nav.append({"text": "След. неделя ▶️", "callback_data": f"w:{next_e:%Y-%m-%d}"})
    return {"inline_keyboard": [nav,
                                [{"text": "📊 Отчёт за день", "callback_data": f"d:{end:%Y-%m-%d}"}],
                                links_row()]}


def tg_send(chat_id: str, text: str, markup: dict | None = None) -> None:
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if markup:
        params["reply_markup"] = json.dumps(markup)
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


def notify(role: str, subject: str, body_html: str, markup: dict | None = None) -> int:
    """role: report - всем подписчикам; alert - только подписанным на тревоги."""
    sent = 0
    if os.getenv("TG_TOKEN"):
        for cid in recipients(role):
            try:
                tg_send(cid, body_html, markup)
                sent += 1
            except Exception:
                pass
    mail_to = os.getenv("REPORT_TO" if role == "report" else "ALERT_TO", "")
    if os.getenv("SMTP_USER") and mail_to:
        send_mail(mail_to, subject, plain(body_html))
        sent += 1
    return sent


# --- Доступы ------------------------------------------------------------------
#
# Роли: owner - владелец (тех. поддержка, получает уведомления о сбоях),
#       admin - управляет доступом и видит отчёты,
#       viewer - только смотрит отчёты.
# Попасть в бот можно только по одноразовой ссылке-приглашению (24 часа)
# или по запросу, который одобрил админ.

ROLE_NAMES = {"owner": "👑 Владелец", "admin": "🛡 Админ", "viewer": "👁 Просмотр"}
INVITE_TTL = 24 * 3600


def acl_tables(con):
    con.execute("create table if not exists tg_users (chat_id text primary key, role text, "
                "alerts integer, label text, added_at integer)")
    con.execute("create table if not exists tg_invites (code text primary key, role text, "
                "created_by text, expires integer, used_by text)")
    con.execute("create table if not exists tg_requests (chat_id text primary key, label text, "
                "created integer)")
    con.execute("create table if not exists kv (k text primary key, v text)")


def subs_table(con):  # совместимость со старым именем
    acl_tables(con)


def get_user(con, cid: str):
    row = con.execute("select role, alerts, label from tg_users where chat_id = ?", (cid,)).fetchone()
    return {"role": row[0], "alerts": row[1], "label": row[2]} if row else None


def is_admin(u) -> bool:
    return bool(u) and u["role"] in ("owner", "admin")


def add_user(con, cid: str, role: str, label: str) -> None:
    alerts = 1 if role == "owner" else 0
    con.execute("insert or replace into tg_users values (?, ?, ?, ?, ?)",
                (cid, role, alerts, label, int(time.time())))
    con.execute("delete from tg_requests where chat_id = ?", (cid,))


def label_of(frm: dict) -> str:
    """Как показать человека в списке доступов: @username или ID."""
    return f"@{frm['username']}" if frm.get("username") else f"ID {frm.get('id')}"


def new_code() -> str:
    import secrets
    return secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")


def bot_username() -> str:
    with db() as con:
        acl_tables(con)
        row = con.execute("select v from kv where k = 'bot_username'").fetchone()
        if row:
            return row[0]
        name = tg("getMe")["result"]["username"]
        con.execute("insert or replace into kv values ('bot_username', ?)", (name,))
        return name


def create_invite(con, role: str, by: str) -> str:
    code = new_code()
    con.execute("insert into tg_invites values (?, ?, ?, ?, null)",
                (code, role, by, int(time.time()) + INVITE_TTL))
    return code


def owner_link() -> str:
    """Ссылка для первого входа владельца. Работает, пока владельца нет."""
    with db() as con:
        acl_tables(con)
        if con.execute("select 1 from tg_users where role = 'owner'").fetchone():
            return "владелец уже назначен"
        row = con.execute("select v from kv where k = 'owner_code'").fetchone()
        code = row[0] if row else new_code()
        con.execute("insert or replace into kv values ('owner_code', ?)", (code,))
    return f"https://t.me/{bot_username()}?start=own_{code}"


def recipients(role: str) -> list:
    """report - все, у кого есть доступ; alert - только с включёнными уведомлениями о сбоях."""
    with db() as con:
        acl_tables(con)
        q = "select chat_id from tg_users" + (" where alerts = 1" if role == "alert" else "")
        return [r[0] for r in con.execute(q)]


def access_view(con, viewer_cid: str) -> tuple:
    users = con.execute("select chat_id, role, label from tg_users order by "
                        "case role when 'owner' then 0 when 'admin' then 1 else 2 end, added_at").fetchall()
    viewer_role = next((r for c, r, _ in users if c == viewer_cid), "")
    L = ["<b>👥 Доступ к отчётам</b>", ""]
    kb = []
    for cid, role, label in users:
        me = " · <i>вы</i>" if cid == viewer_cid else ""
        L.append(f"{ROLE_NAMES[role]} - {html.escape(label)}{me}")
        if role != "owner" and cid != viewer_cid:
            kb.append([{"text": f"🗑 Убрать {label}", "callback_data": f"rm:{cid}"}])
            if viewer_role == "owner" and role == "admin":
                kb.append([{"text": f"👑 Сделать владельцем {label}", "callback_data": f"own:{cid}"}])
    pending = con.execute("select count(*) from tg_requests").fetchone()[0]
    if pending:
        L += ["", f"🙋 Ожидают одобрения: {pending}"]
    L += ["", "Приглашение - одноразовая ссылка, действует 24 часа."]
    kb.append([{"text": "➕ Пригласить для просмотра", "callback_data": "inv:viewer"}])
    kb.append([{"text": "➕ Пригласить админа", "callback_data": "inv:admin"}])
    return "\n".join(L), {"inline_keyboard": kb}


def notify_admins(text: str, markup: dict | None = None, except_cid: str = "") -> None:
    with db() as con:
        acl_tables(con)
        admins = [r[0] for r in con.execute("select chat_id from tg_users where role in ('owner','admin')")]
    for cid in admins:
        if cid != except_cid:
            try:
                tg_send(cid, text, markup)
            except Exception:
                pass


def menu_for(u) -> dict:
    rows = [[{"text": BTN_TODAY}, {"text": BTN_YESTERDAY}],
            [{"text": BTN_WEEK}, {"text": BTN_HELP}]]
    if is_admin(u):
        rows.append([{"text": BTN_ACCESS}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True,
            "input_field_placeholder": "Выберите отчёт 👇"}


def welcome(cid: str, u) -> None:
    lines = [f"✅ <b>Доступ открыт.</b> Ваша роль: {ROLE_NAMES[u['role']]}"]
    if u["role"] == "owner":
        lines.append("🔔 Сюда же придут уведомления, если что-то сломается.")
    if is_admin(u):
        lines.append(f"Кнопка {BTN_ACCESS} - кто видит отчёты, приглашения и запросы.")
    tg_send(cid, "\n".join(lines) + "\n\n" + HELP, menu_for(u))
    tg_send(cid, *day_view(datetime.now(MSK) - timedelta(days=1)))


# --- Бот: меню, кнопки, приглашения -------------------------------------------

HELP = ("<b>👋 Бот отчётов автовебинара</b>\n\n"
        "🕘 Каждое утро в 9:00 присылаю итоги за вчера.\n"
        "📅 По понедельникам - итоги прошлой недели.\n\n"
        "<b>Кнопки внизу:</b>\n"
        f"{BTN_TODAY} - цифры на текущий момент\n"
        f"{BTN_YESTERDAY} - итоги за вчера\n"
        f"{BTN_WEEK} - итоги за 7 дней\n\n"
        "В отчёте видно, сколько сообщений написали в чат эфира.\n"
        "Под отчётами можно листать дни ◀️ ▶️ и сразу открыть Бизон или Сейлбот.")

LOCKED = ("🔒 <b>Отчёты автовебинара - доступ по приглашению.</b>\n\n"
          "Если вы из команды, нажмите кнопку ниже - администратор получит запрос "
          "и откроет доступ.")


def day_view(day: datetime) -> tuple:
    now = datetime.now(MSK)
    if day.date() == now.date():
        title = f"📊 Сегодня, {human_day(day)} · на {now:%H:%M}"
        return render_day(day_stats(day), None, title), day_keyboard(day)
    title = f"📊 Итоги за {human_day(day)}"
    prev = day_stats(day - timedelta(days=1), with_room=False)
    return render_day(day_stats(day), prev, title), day_keyboard(day)


def week_view(end: datetime) -> tuple:
    return render_week(end), week_keyboard(end)


def parse_day(s: str) -> datetime:
    d = datetime.strptime(s, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, 12, tzinfo=MSK)


def setup_bot() -> None:
    tg("setMyCommands", commands=json.dumps([
        {"command": "today", "description": "📊 Цифры за сегодня"},
        {"command": "yesterday", "description": "🗓 Итоги за вчера"},
        {"command": "week", "description": "📈 Итоги за неделю"},
        {"command": "help", "description": "ℹ️ Что умеет бот"}]))
    tg("setMyShortDescription",
       short_description="Отчёты по автовебинару: записи, доходимость, досмотры. Доступ по приглашению.")
    tg("setMyDescription", description=(
        "Каждое утро присылаю итоги автовебинара: сколько записалось, пришло и досмотрело.\n\n"
        "🔒 Доступ только по приглашению администратора."))


def on_start(con, cid: str, arg: str, frm: dict) -> tuple:
    """Обработка /start с параметром. Возвращает действие для ответа."""
    u = get_user(con, cid)
    if arg.startswith("own_"):
        row = con.execute("select v from kv where k = 'owner_code'").fetchone()
        has_owner = con.execute("select 1 from tg_users where role = 'owner'").fetchone()
        if row and not has_owner and arg[4:] == row[0]:
            add_user(con, cid, "owner", label_of(frm))
            con.execute("delete from kv where k = 'owner_code'")
            return ("welcome", cid)
        return ("expired", cid)
    if arg.startswith("inv_"):
        inv = con.execute("select role, created_by, expires, used_by from tg_invites where code = ?",
                          (arg[4:],)).fetchone()
        if not inv or inv[3] or inv[2] < time.time():
            return ("welcome", cid) if u else ("expired", cid)
        role, by = inv[0], inv[1]
        if u and u["role"] in ("owner", "admin") and role == "viewer":
            return ("welcome", cid)  # не понижаем админа по старой ссылке
        con.execute("update tg_invites set used_by = ? where code = ?", (cid, arg[4:]))
        add_user(con, cid, role if not u or u["role"] != "owner" else "owner", label_of(frm))
        return ("joined", (cid, by, label_of(frm), role))
    return ("welcome", cid) if u else ("locked", cid)


def process(updates: list, con) -> list:
    """Разбирает пачку обновлений. Все изменения доступа - здесь, ответы - потом."""
    todo = []
    for upd in updates:
        if upd.get("callback_query"):
            q = upd["callback_query"]
            cid = str(q.get("message", {}).get("chat", {}).get("id"))
            data, u = q.get("data") or "", get_user(con, cid)
            if data == "req":
                if u:
                    todo.append(("cbmsg", (q, "У вас уже есть доступ")))
                    continue
                frm = q.get("from") or {}
                fresh = not con.execute("select 1 from tg_requests where chat_id = ?", (cid,)).fetchone()
                con.execute("insert or replace into tg_requests values (?, ?, ?)",
                            (cid, label_of(frm), int(time.time())))
                todo.append(("requested", (q, cid, label_of(frm), fresh)))
            elif not u:
                todo.append(("cbmsg", (q, "Нет доступа")))
            elif data.startswith("own:") and u["role"] != "owner":
                todo.append(("cbmsg", (q, "Это может только владелец")))
            elif data.startswith("own:"):
                target, _, confirmed = data[4:].partition(":")
                t = get_user(con, target)
                if confirmed != "yes":
                    todo.append(("own_ask", (q, target, t["label"] if t else "")))
                    continue
                if not t or t["role"] != "admin":
                    todo.append(("cbmsg", (q, "Передать можно только админу")))
                    continue
                # новый владелец получает и уведомления о сбоях; прежний остаётся админом
                con.execute("update tg_users set role = 'owner', alerts = 1 where chat_id = ?", (target,))
                con.execute("update tg_users set role = 'admin' where chat_id = ?", (cid,))
                todo.append(("owned", (q, cid, target, t["label"])))
            elif data.startswith(("ok:", "no:", "rm:", "inv:")) and not is_admin(u):
                todo.append(("cbmsg", (q, "Это может только админ")))
            elif data.startswith(("ok:", "no:")):
                parts = data.split(":")
                target, role = parts[1], (parts[2] if len(parts) > 2 else "")
                req = con.execute("select label from tg_requests where chat_id = ?", (target,)).fetchone()
                if not req:
                    todo.append(("cbmsg", (q, "Запрос уже обработан")))
                    continue
                if data.startswith("ok:"):
                    add_user(con, target, role, req[0])
                else:
                    con.execute("delete from tg_requests where chat_id = ?", (target,))
                todo.append(("decided", (q, target, req[0], role if data.startswith("ok:") else "")))
            elif data.startswith("rm:"):
                target = data[3:]
                t = get_user(con, target)
                if t and t["role"] != "owner":
                    con.execute("delete from tg_users where chat_id = ?", (target,))
                    todo.append(("removed", (q, cid, target, t["label"])))
                else:
                    todo.append(("cbmsg", (q, "Нельзя удалить")))
            elif data.startswith("inv:"):
                role = data[4:] if data[4:] in ("viewer", "admin") else "viewer"
                todo.append(("invite", (q, cid, create_invite(con, role, cid), role)))
            elif data == "acc":
                todo.append(("access", (q, cid)))
            else:
                todo.append(("cb", q))
            continue
        m = upd.get("message") or {}
        text, chat = (m.get("text") or "").strip(), m.get("chat") or {}
        if not chat or chat.get("type") != "private":
            continue
        cid, frm = str(chat["id"]), m.get("from") or {}
        if text.startswith("/start"):
            parts = text.split()
            todo.append(on_start(con, cid, parts[1] if len(parts) > 1 else "", frm))
            continue
        u = get_user(con, cid)
        if not u:
            todo.append(("locked", cid))
        elif text == BTN_ACCESS and is_admin(u):
            todo.append(("access_msg", cid))
        else:
            todo.append(("text", (cid, text)))
    return todo


def handle_text(cid: str, text: str) -> None:
    now = datetime.now(MSK)
    if text in (BTN_TODAY, "/today"):
        tg_send(cid, *day_view(now))
    elif text in (BTN_YESTERDAY, "/yesterday"):
        tg_send(cid, *day_view(now - timedelta(days=1)))
    elif text in (BTN_WEEK, "/week"):
        tg_send(cid, *week_view(now - timedelta(days=1)))
    else:
        with db() as con:
            acl_tables(con)
            u = get_user(con, cid)
        tg_send(cid, HELP, menu_for(u))


def handle_callback(q: dict) -> None:
    data, msg = q.get("data") or "", q.get("message") or {}
    cid, mid = str(msg.get("chat", {}).get("id")), msg.get("message_id")
    try:
        kind, arg = data.split(":", 1)
        day = parse_day(arg)
        if day.date() > datetime.now(MSK).date():
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Этот день ещё не наступил")
            return
        if kind == "c":
            text, markup = render_chat(day, day_stats(day)), chat_keyboard(day)
        else:
            text, markup = day_view(day) if kind == "d" else week_view(day)
        tg("answerCallbackQuery", callback_query_id=q["id"], text="⏳ Обновляю…")
        edit(cid, mid, text, markup)
    except Exception:
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Не получилось, попробуйте ещё раз")


def edit(cid, mid, text: str, markup: dict | None) -> None:
    try:
        tg("editMessageText", chat_id=cid, message_id=mid, text=text, parse_mode="HTML",
           disable_web_page_preview="true", reply_markup=json.dumps(markup or {"inline_keyboard": []}))
    except Exception:
        pass  # message is not modified


def refresh_access(q: dict, cid: str) -> None:
    with db() as con:
        acl_tables(con)
        text, markup = access_view(con, cid)
    edit(cid, q["message"]["message_id"], text, markup)


def answer(todo: list) -> None:
    for kind, arg in todo:
        try:
            if kind == "welcome":
                with db() as con:
                    acl_tables(con)
                    u = get_user(con, arg)
                welcome(arg, u)
            elif kind == "joined":
                cid, by, label, role = arg
                with db() as con:
                    acl_tables(con)
                    u = get_user(con, cid)
                welcome(cid, u)
                tg_send(by, f"✅ По вашему приглашению подключился {html.escape(label)} - "
                            f"{ROLE_NAMES[role]}.")
            elif kind == "expired":
                tg_send(arg, "⌛️ Ссылка уже использована или устарела.\n\n" + LOCKED,
                        {"inline_keyboard": [[{"text": "🙋 Запросить доступ", "callback_data": "req"}]]})
            elif kind == "locked":
                tg_send(arg, LOCKED,
                        {"inline_keyboard": [[{"text": "🙋 Запросить доступ", "callback_data": "req"}]]})
            elif kind == "requested":
                q, cid, label, fresh = arg
                tg("answerCallbackQuery", callback_query_id=q["id"], text="Запрос отправлен ✅")
                edit(cid, q["message"]["message_id"],
                     "📨 <b>Запрос отправлен.</b>\nКак только администратор одобрит, я сразу напишу.", None)
                if fresh:
                    notify_admins(
                        f"🙋 <b>Запрос доступа к отчётам</b>\nОт: {html.escape(label)}",
                        {"inline_keyboard": [
                            [{"text": "✅ Разрешить просмотр", "callback_data": f"ok:{cid}:viewer"}],
                            [{"text": "🛡 Сделать админом", "callback_data": f"ok:{cid}:admin"},
                             {"text": "❌ Отклонить", "callback_data": f"no:{cid}"}]]})
            elif kind == "decided":
                q, target, label, role = arg
                verdict = f"✅ {html.escape(label)} - {ROLE_NAMES[role]}" if role else \
                          f"❌ Запрос {html.escape(label)} отклонён"
                tg("answerCallbackQuery", callback_query_id=q["id"], text="Готово")
                edit(str(q["message"]["chat"]["id"]), q["message"]["message_id"], verdict, None)
                if role:
                    with db() as con:
                        acl_tables(con)
                        u = get_user(con, target)
                    welcome(target, u)
                else:
                    tg_send(target, "К сожалению, в доступе отказано.")
            elif kind == "own_ask":
                q, target, label = arg
                tg("answerCallbackQuery", callback_query_id=q["id"])
                edit(q["message"]["chat"]["id"], q["message"]["message_id"],
                     f"👑 Передать роль владельца {html.escape(label)}?\n\n"
                     "Вы останетесь админом и сможете дальше смотреть отчёты и приглашать людей. "
                     "Новый владелец будет получать уведомления о сбоях и сможет убрать любого.",
                     {"inline_keyboard": [[{"text": "✅ Да, передать", "callback_data": f"own:{target}:yes"}],
                                          [{"text": "↩️ Отмена", "callback_data": "acc"}]]})
            elif kind == "owned":
                q, cid, target, label = arg
                tg("answerCallbackQuery", callback_query_id=q["id"], text=f"Владелец теперь {label}")
                refresh_access(q, cid)
                with db() as con:
                    acl_tables(con)
                    tu = get_user(con, target)
                tg_send(target, "👑 Вам передали роль владельца отчётов. Сюда же будут приходить "
                                "уведомления, если что-то сломается.", menu_for(tu))
            elif kind == "removed":
                q, cid, target, label = arg
                tg("answerCallbackQuery", callback_query_id=q["id"], text=f"Доступ {label} убран")
                refresh_access(q, cid)
                tg_send(target, "🔒 Доступ к отчётам отключён.", {"remove_keyboard": True})
            elif kind == "invite":
                q, cid, code, role = arg
                tg("answerCallbackQuery", callback_query_id=q["id"], text="Ссылка готова")
                who = "админа" if role == "admin" else "для просмотра"
                tg_send(cid, f"🔗 <b>Приглашение {who}</b>\n\n"
                             f"https://t.me/{bot_username()}?start=inv_{code}\n\n"
                             "Перешлите ссылку человеку. Она одноразовая и действует 24 часа.")
            elif kind == "access":
                q, cid = arg
                tg("answerCallbackQuery", callback_query_id=q["id"])
                refresh_access(q, cid)
            elif kind == "access_msg":
                with db() as con:
                    acl_tables(con)
                    text, markup = access_view(con, arg)
                tg_send(arg, text, markup)
            elif kind == "cbmsg":
                q, text = arg
                tg("answerCallbackQuery", callback_query_id=q["id"], text=text)
            elif kind == "cb":
                handle_callback(arg)
            else:
                handle_text(*arg)
        except Exception:
            pass


def poll(wait: int = 0) -> None:
    """Одна итерация: забрать обновления (wait - long polling, сек) и ответить."""
    with db() as con:
        acl_tables(con)
        row = con.execute("select v from kv where k = 'tg_offset'").fetchone()
        offset = int(row[0]) if row else 0
        need_setup = not con.execute("select 1 from kv where k = 'tg_setup_v3'").fetchone()
    if need_setup:
        setup_bot()
        with db() as con:
            con.execute("insert or replace into kv values ('tg_setup_v3', '1')")
    updates = tg("getUpdates", _http_timeout=wait + 15, offset=offset, timeout=wait,
                 allowed_updates=json.dumps(["message", "callback_query"])).get("result", [])
    if not updates:
        return
    with db() as con:
        acl_tables(con)
        todo = process(updates, con)
        con.execute("insert or replace into kv values ('tg_offset', ?)",
                    (str(max(u["update_id"] for u in updates) + 1),))
    answer(todo)  # отвечаем после сохранения - без повторов при сбое


def _stuck(signum, frame):
    raise TimeoutError("обработка обновлений зависла")


def serve() -> None:
    """Постоянная работа бота: кнопки отвечают мгновенно.

    Каждая итерация ограничена по времени: если Telegram или Бизон
    подвисли на сетевом запросе, бот не застрянет, а пойдёт дальше.
    """
    import signal
    import traceback
    signal.signal(signal.SIGALRM, _stuck)
    while True:
        signal.alarm(180)
        try:
            poll(wait=25)
        except Exception:
            traceback.print_exc()
            time.sleep(5)
        finally:
            signal.alarm(0)


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
    notify("report", f"Автовебинар: итоги за {y:%d.%m}", text, day_keyboard(y))
    if week:
        notify("report", "Автовебинар: итоги недели", week, week_keyboard(y))


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
           "\n\nЕсли токен меняли - обновите секрет BIZON_TOKEN в GitHub и перезапустите деплой.")
    open(mark, "w").close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "daily":
        daily("--dry" in sys.argv)
    elif cmd == "watchdog":
        watchdog()
    elif cmd == "serve":
        if os.getenv("TG_TOKEN"):
            serve()
        else:
            time.sleep(3600)  # без токена просто ждём; systemd перезапустит после деплоя
    elif cmd == "test":
        print("sent=%d" % notify("report", "Проверка связи",
                                 "🔔 <b>Проверка связи</b>\nОтчёты автовебинара будут приходить сюда."))
    elif cmd == "links":
        print(owner_link())
    else:
        print(__doc__)
