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
