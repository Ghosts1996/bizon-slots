from datetime import datetime

from app.main import MSK, build, resolve


def t(h, m=0, s=0, d=30):
    return datetime(2026, 10, d, h, m, s, tzinfo=MSK)


def test_morning_all_four_today():
    r = build(t(9))
    assert r["today_count"] == 4
    assert r["t1"] == "Сегодня 14:00 по МСК"
    assert r["m4"] == "Завтра 21:00 по МСК"


def test_slot_hidden_right_after_start():
    r = build(t(14, 0, 1))
    assert [x["time"] for x in r["today"]] == ["16:00", "19:00", "21:00"]


def test_slot_visible_one_minute_before():
    assert build(t(13, 59))["t1"] == "Сегодня 14:00 по МСК"


def test_after_last_slot_only_tomorrow():
    r = build(t(21, 30))
    assert r["has_today"] == 0 and r["t1"] == ""
    assert len(r["tomorrow"]) == 4


def test_resolve_passed():
    assert resolve("Сегодня 14:00 по МСК", t(14, 1))["valid"] == 0


def test_resolve_ok_and_delays():
    r = resolve("Сегодня 16:00 по МСК", t(15, 30))
    assert r["valid"] == 1 and r["remind_60"] == 0 and r["delay_7"] == 1380
    assert r["time_utc"] == "2026-10-30T13:00:00.000Z"


def test_resolve_tomorrow():
    r = resolve("Завтра 14:00 по МСК", t(22))
    assert r["date"] == "31.10.2026" and r["remind_60"] == 1


def test_upcoming_buttons_always_four():
    r = build(t(20))
    assert [r[f"b{i}"] for i in range(1, 5)] == [
        "Сегодня 21:00 по МСК", "Завтра 14:00 по МСК", "Завтра 16:00 по МСК", "Завтра 19:00 по МСК"]
    r = build(t(22))
    assert r["b1"] == "Завтра 14:00 по МСК" and r["b4"] == "Завтра 21:00 по МСК"


def test_resolve_by_ts():
    from app.main import resolve_ts
    ts = build(t(13))["bts1"]
    assert resolve_ts(ts, t(13, 30))["valid"] == 1
    assert resolve_ts(ts, t(14, 0, 1))["valid"] == 0


def test_resolve_msg_fields():
    ok = resolve("Завтра 14:00 по МСК", t(22))
    assert ok["valid"] == 1 and "вы записаны" in ok["msg"] and ok["room_url"]
    bad = resolve("ерунда", t(22))
    assert bad["valid"] == 0 and bad["msg"] and bad["delay_7"] == 0


def test_gap_everyone_gets_both():
    early = resolve("Сегодня 21:00 по МСК", t(17, 15))
    assert early["delay_60"] + early["delay_gap"] == early["delay_5"] and early["delay_gap"] == 3300
    late = resolve("Сегодня 21:00 по МСК", t(20, 50))
    assert late["delay_60"] == 0 and late["delay_gap"] == 300 and late["remind_head"].startswith("Совсем")
    last = resolve("Сегодня 21:00 по МСК", t(20, 58))
    assert last["delay_gap"] == 0


def test_dedup_chain(tmp_path, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    ts19 = int(t(19).timestamp()); ts16 = int(t(16, d=31).timestamp())
    m.save_registration("c1", ts19, int(t(18, 7).timestamp()))
    # передумал и записался на завтра 16:00
    m.save_registration("c1", ts16, int(t(18, 20).timestamp()))
    # старая цепочка 19:00: шаг "ссылка" в 18:55 - уже не актуален
    assert m.claim_step("c1", "link", int(t(18, 55).timestamp())) is None
    # новая цепочка: напоминание завтра в 15:00 - один раз
    assert m.claim_step("c1", "hour", int(t(15, 0, 30, d=31).timestamp())) == ts16
    assert m.claim_step("c1", "hour", int(t(15, 1, d=31).timestamp())) is None
    assert m.claim_step("c1", "link", int(t(15, 55, d=31).timestamp())) == ts16
    assert m.claim_step("c1", "check", int(t(17, 30, d=31).timestamp())) == ts16


def test_late_registration_immediate_hour(tmp_path, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    ts = int(t(21).timestamp())
    m.save_registration("c2", ts, int(t(20, 40).timestamp()))
    assert m.claim_step("c2", "hour", int(t(20, 40, 5).timestamp())) == ts
    assert m.claim_step("c2", "link", int(t(20, 55).timestamp())) == ts


def test_segments():
    from app.main import segment, watched_minutes
    assert watched_minutes({"vi": [{"s": 2, "e": 4237}]}) > 70
    assert watched_minutes({"view": 0, "viewTill": 600000}) == 10
    assert segment(False, 0) == "none" and segment(True, 5) == "early"
    assert segment(True, 30) == "left" and segment(True, 65) == "full"


def test_registered_for_moves(tmp_path, monkeypatch):
    import app.main as m
    from app.report import registered_for
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    ts19, ts21 = int(t(19).timestamp()), int(t(21).timestamp())
    m.save_registration("a", ts19, int(t(10).timestamp()))
    m.save_registration("b", ts19, int(t(11).timestamp()))
    m.save_registration("b", ts21, int(t(12).timestamp()))  # перенёс на 21:00
    with m.db() as con:
        assert registered_for(con, ts19) == {"a"}
        assert registered_for(con, ts21) == {"b"}


def test_followup_again_then_record(tmp_path, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    clock = {"now": t(13)}
    monkeypatch.setattr(m, "now_msk", lambda: clock["now"])

    # первый пропуск 14:00 → снова выбор времени, без записи
    ts1 = int(t(14).timestamp())
    m.save_visit("c1", ts1, 0, 0, "none")
    clock["now"] = t(15, 30)
    r = m.followup("c1", False)
    assert r["again"] == 1 and r["record"] == 0

    # второй пропуск подряд → запись завтра в 10:00
    ts2 = int(t(19).timestamp())
    m.save_registration("c1", ts2, int(t(15, 31).timestamp()))
    m.save_visit("c1", ts2, 0, 0, "none")
    clock["now"] = t(20, 30)
    r = m.followup("c1", False)
    assert r["again"] == 0 and r["record"] == 1
    assert r["delay_record"] == int((t(10, d=31) - t(20, 30)).total_seconds())

    # утром запись уходит один раз
    assert m.claim_step("c1", "record", int(t(10, d=31).timestamp())) == ts2
    assert m.claim_step("c1", "record", int(t(10, 1, d=31).timestamp())) is None


def test_followup_resets_after_visit(tmp_path, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    m.save_visit("c2", int(t(14).timestamp()), 0, 0, "none")
    m.save_visit("c2", int(t(16).timestamp()), 1, 70, "full")
    m.save_visit("c2", int(t(19).timestamp()), 0, 0, "none")
    assert m.followup("c2", False)["again"] == 1
    assert m.followup("c2", True) == {}


def test_record_skipped_after_reregistration(tmp_path, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    ts = int(t(19).timestamp())
    m.save_registration("c3", ts, int(t(12).timestamp()))
    # человек сам записался заново на завтра - утренняя запись не нужна
    m.save_registration("c3", int(t(14, d=31).timestamp()), int(t(21).timestamp()))
    assert m.claim_step("c3", "record", int(t(10, d=31).timestamp())) is None


def test_msg_and_calendar():
    r = resolve("Завтра 16:00 по МСК", t(12))
    assert "📅 Завтра 31.10.2026 в 16:00 по МСК." in r["msg"]
    assert "dates=20261031T130000Z%2F20261031T143000Z" in r["cal_url"]
    assert resolve("Сегодня 14:00 по МСК", t(15))["cal_url"] == ""


def test_empty_room_counts_as_missed(tmp_path, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(m, "bizon_get", lambda path, params: {"list": [], "viewers": [], "total": 0})
    ts = int(t(14).timestamp())
    monkeypatch.setattr(m, "now_msk", lambda: t(14, 50))
    assert m.check_visit("c9", ts)["ready"] == 0          # эфир ещё мог идти
    monkeypatch.setattr(m, "now_msk", lambda: t(15, 30))
    r = m.check_visit("c9", ts)
    assert r["ready"] == 1 and r["visited"] == 0 and r["again"] == 1


def test_chat_from_bizon_report(monkeypatch):
    import json
    from app import report as r
    rep = {"report": json.dumps({"usersMeta": {
               "A": {"username": "Ооо", "url": "https://x/room?salebot=1"},
               "B": {"username": "Тест <b>", "url": "https://x/room"}}}),
           "messages": json.dumps({"A": ["Опа", "Второе"], "B": ["Сбда"]}),
           "messagesTS": json.dumps({"A": [1695, 1800], "B": [1731]})}
    monkeypatch.setattr(r, "bizon_get", lambda path, params: {"report": rep})
    r._chat_cache.clear()
    chat = r.session_chat(t(21, d=2))
    assert [m["text"] for m in chat] == ["Опа", "Сбда", "Второе"]
    assert chat[0]["bot"] and not chat[1]["bot"]
    line = r.chat_line(dict(chat[1], hour=21))
    assert line == "<code>21:00 · 28:51</code> <b>Тест &lt;b&gt;</b>: Сбда"
    assert r.plural(1, "а", "б", "в") == "а" and r.plural(3, "а", "б", "в") == "б" and r.plural(11, "а", "б", "в") == "в"


def test_chat_render_fits_telegram(monkeypatch):
    from app import report as r
    msgs = [{"sec": i, "name": "Имя", "text": "x" * 200, "bot": False} for i in range(100)]
    st = {"chat": [dict(m, hour=14) for m in msgs],
          "slots": [{"hour": 14, "chat": msgs}]}
    text = r.render_chat(t(12), st)
    assert len(text) < 4096 and "последние" in text


def test_owner_transfer(tmp_path, monkeypatch):
    import app.main as m
    from app import report as r
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "t.db"))
    con = m.db(); r.acl_tables(con)
    r.add_user(con, "1", "owner", "@me"); r.add_user(con, "2", "admin", "@new")
    _, kb = r.access_view(con, "1")
    assert any(b["callback_data"] == "own:2" for row in kb["inline_keyboard"] for b in row)
    q = {"id": "x", "data": "own:2", "message": {"chat": {"id": 1}, "message_id": 5}}
    assert r.process([{"callback_query": q}], con)[0][0] == "own_ask"
    q["data"] = "own:2:yes"
    assert r.process([{"callback_query": q}], con)[0][0] == "owned"
    assert r.get_user(con, "2")["role"] == "owner" and r.get_user(con, "1")["role"] == "admin"
    q["data"] = "own:1:yes"   # бывший владелец больше не может
    assert r.process([{"callback_query": q}], con)[0][1][1] == "Это может только владелец"
