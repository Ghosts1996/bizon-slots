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
    # старая цепочка 19:00: шаг "ссылка" в 18:55 — уже не актуален
    assert m.claim_step("c1", "link", int(t(18, 55).timestamp())) is None
    # новая цепочка: напоминание завтра в 15:00 — один раз
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
