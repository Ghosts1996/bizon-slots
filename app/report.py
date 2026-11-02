"""Ежедневный отчёт по автовебинару и сторож.

python -m app.report daily [--dry]   — отчёт за вчера на REPORT_TO
python -m app.report watchdog        — проверка токена Бизона, тревога на ALERT_TO
"""
import os
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
    send(os.getenv("REPORT_TO", ""), f"Автовебинар: итоги за {day:%d.%m}", text)


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
            send(os.getenv("ALERT_TO", ""), "✅ Автовебинар: связь с Бизоном восстановлена",
                 "Проверка посещаемости снова работает.")
        return
    # не спамим: одно письмо раз в 6 часов
    if os.path.exists(mark) and time.time() - os.path.getmtime(mark) < 6 * 3600:
        return
    send(os.getenv("ALERT_TO", ""), "⚠️ Автовебинар: проблема с Бизоном",
         problem + "\n\nЕсли токен меняли — обновите секрет BIZON_TOKEN в GitHub и перезапустите деплой.")
    open(mark, "w").close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "daily":
        daily("--dry" in sys.argv)
    elif cmd == "watchdog":
        watchdog()
    else:
        print(__doc__)
