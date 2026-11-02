"""Ежедневный отчёт по автовебинару и сторож.

Доставка — Telegram-бот (TG_TOKEN). Подписка по ссылкам:
  t.me/<бот>?start=report — получать ежедневный отчёт
  t.me/<бот>?start=alert  — получать отчёт и тревоги
Если задан SMTP_*, дополнительно дублирует на почту.

python -m app.report daily [--dry]   — отчёт за вчера
python -m app.report watchdog        — проверка токена Бизона
python -m app.report poll            — принять новые подписки в боте
python -m app.report test            — тестовое сообщение всем подписчикам
"""
import json
import os
import urllib.parse
import urllib.request
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.message import EmailMessage

from app.main import MSK, ROOM_ID, SLOTS, at, bizon_get, db, session_viewers

STATE_DIR = os.getenv("STATE_DIR", "/opt/bizon-slots")
SEG_NAMES = {"early": "ушли в начале", "left": "ушли до предложения", "full": "досмотрели"}


def send(to: str, subject: str, body: str) -> None:
    user, pwd = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
    if not (user and pwd and to):
        raise RuntimeError("SMTP_USER / SMTP_PASS / получатель не заданы")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    host = os.getenv("SMTP_HOST", "smtp.yandex.ru")
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465")), timeout=30) as smtp:
        smtp.login(user, pwd)
        smtp.send_message(msg)


# --- Telegram -----------------------------------------------------------------

def tg(method: str, **params) -> dict:
    token = os.getenv("TG_TOKEN", "")
    if not token:
        raise RuntimeError("TG_TOKEN не задан")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def subs_table(con):
    con.execute("create table if not exists tg_subs (chat_id text, role text, name text, "
                "primary key (chat_id, role))")
    con.execute("create table if not exists kv (k text primary key, v text)")


def poll() -> None:
    """Забирает новые сообщения бота и оформляет подписки по /start report|alert."""
    with db() as con:
        subs_table(con)
        row = con.execute("select v from kv where k = 'tg_offset'").fetchone()
        offset = int(row[0]) if row else 0
        upd = tg("getUpdates", offset=offset, timeout=0).get("result", [])
        for u in upd:
            offset = max(offset, u["update_id"] + 1)
            m = u.get("message") or {}
            text, chat = (m.get("text") or "").strip(), m.get("chat") or {}
            if not chat:
                continue
            cid = str(chat["id"])
            name = chat.get("first_name") or chat.get("title") or ""
            role = text.split()[1] if text.startswith("/start ") and len(text.split()) > 1 else ""
            if role in ("report", "alert"):
                con.execute("insert or replace into tg_subs values (?, ?, ?)", (cid, role, name))
                what = ("ежедневный отчёт по автовебинару (каждое утро в 9:00)" if role == "report"
                        else "отчёты и уведомления о сбоях")
                tg("sendMessage", chat_id=cid, text=f"Готово ✅ Вы подписаны на {what}.")
            elif text in ("/stop", "Отписаться"):
                con.execute("delete from tg_subs where chat_id = ?", (cid,))
                tg("sendMessage", chat_id=cid, text="Вы отписались от уведомлений.")
            elif text.startswith("/start"):
                tg("sendMessage", chat_id=cid, text="Это служебный бот отчётов автовебинара. "
                   "Для подписки используйте ссылку, которую вам прислали.")
        con.execute("insert or replace into kv values ('tg_offset', ?)", (str(offset),))


def recipients(role: str) -> list:
    with db() as con:
        subs_table(con)
        roles = ("report", "alert") if role == "report" else ("alert",)
        q = "select distinct chat_id from tg_subs where role in (%s)" % ",".join("?" * len(roles))
        return [r[0] for r in con.execute(q, roles)]


def notify(role: str, subject: str, body: str) -> int:
    """role: report — всем подписчикам; alert — только тем, кто на тревогах."""
    sent = 0
    if os.getenv("TG_TOKEN"):
        try:
            poll()
        except Exception:
            pass
        for cid in recipients(role):
            try:
                tg("sendMessage", chat_id=cid, text=f"{subject}\n\n{body}")
                sent += 1
            except Exception:
                pass
    mail_to = os.getenv("REPORT_TO" if role == "report" else "ALERT_TO", "")
    if os.getenv("SMTP_USER") and mail_to:
        send(mail_to, subject, body)
        sent += 1
    return sent


def registered_for(con, ts: int) -> set:
    """Клиенты, у которых на момент начала сеанса последней записью был именно этот сеанс."""
    out = set()
    rows = con.execute("select client_id, ts, reg_at from reg_log where reg_at < ? "
                       "order by reg_at", (ts,)).fetchall()
    last = {}
    for cid, t, _ in rows:
        last[cid] = t
    for cid, t in last.items():
        if t == ts:
            out.add(cid)
    return out


def build_daily(day: datetime) -> str:
    lines, tot_reg, tot_vis = [], 0, 0
    seg_tot = {"early": 0, "left": 0, "full": 0}
    room_total = 0
    with db() as con:
        for h in SLOTS:
            ts = int(at(day, h).timestamp())
            regs = registered_for(con, ts)
            vis = con.execute("select client_id, segment from visits where ts = ? and visited = 1",
                              (ts,)).fetchall()
            came = {c for c, _ in vis if c in regs}
            for _, sg in vis:
                if sg in seg_tot:
                    seg_tot[sg] += 1
            try:
                viewers = len(session_viewers(f"{ROOM_ID}*{at(day, h):%Y-%m-%dT%H:%M:00}"))
            except Exception:
                viewers = 0
            room_total += viewers
            tot_reg += len(regs)
            tot_vis += len(came)
            lines.append(f"  {h:02d}:00 — записались {len(regs)}, пришли {len(came)}"
                         f" (всего зрителей в комнате: {viewers})")
    pct = f" ({round(100 * tot_vis / tot_reg)}%)" if tot_reg else ""
    body = [f"Автовебинар «Как происходит развитие ребёнка» — {day:%d.%m.%Y}", "",
            f"Записались через бота: {tot_reg}",
            f"Пришли на эфир: {tot_vis}{pct}", ""]
    if tot_vis:
        body += ["Как смотрели:"] + [f"  {SEG_NAMES[k]}: {v}" for k, v in seg_tot.items()] + [""]
    body += ["По сеансам:"] + lines + ["",
             f"Всего зрителей в комнате за день: {room_total}",
             "", "Отчёт формируется автоматически каждое утро."]
    return "\n".join(body)


def daily(dry: bool) -> None:
    day = datetime.now(MSK) - timedelta(days=1)
    text = build_daily(day)
    if dry:
        print(text)
        return
    notify("report", f"📊 Автовебинар: итоги за {day:%d.%m}", text)


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
            notify("alert", "✅ Автовебинар: связь с Бизоном восстановлена",
                   "Проверка посещаемости снова работает.")
        return
    # не спамим: одно письмо раз в 6 часов
    if os.path.exists(mark) and time.time() - os.path.getmtime(mark) < 6 * 3600:
        return
    notify("alert", "⚠️ Автовебинар: проблема с Бизоном",
           problem + "\n\nЕсли токен меняли — обновите секрет BIZON_TOKEN в GitHub и перезапустите деплой.")
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
        n = notify("report", "🔔 Проверка связи", "Отчёты автовебинара будут приходить сюда.")
        print(f"sent={n}")
    elif cmd == "links":
        me = tg("getMe")["result"]["username"]
        print(f"https://t.me/{me}?start=report https://t.me/{me}?start=alert")
    else:
        print(__doc__)
