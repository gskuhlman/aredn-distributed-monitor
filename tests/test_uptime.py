"""Node uptime % (uptime.py) and the AREDN uptime string parser."""

from datetime import datetime, timedelta

from observations import _parse_uptime_seconds
import uptime

T0 = datetime(2026, 11, 1, 0, 0, 0)


def polls(n, every_min=5, start=T0, uptime_at_start=86400 * 3, ok=True):
    """n direct polls every `every_min` minutes of a node that keeps running."""
    out = []
    for i in range(n):
        t = start + timedelta(minutes=every_min * i)
        out.append((t, ok, uptime_at_start + every_min * 60 * i if ok else None, True))
    return out


def test_parser_reads_hours_and_minutes():
    assert _parse_uptime_seconds('8 days, 18:37') == 8 * 86400 + 18 * 3600 + 37 * 60
    assert _parse_uptime_seconds('0 days, 1:12') == 4320
    assert _parse_uptime_seconds('1 days, 2:03:04') == 93784
    assert _parse_uptime_seconds('0 days, 0:00') == 0      # just booted, not unknown
    assert _parse_uptime_seconds('junk') is None


def test_always_up():
    up, down, unchecked, reboots = uptime.judge(polls(13), T0)
    assert down == 0 and unchecked == 0 and reboots == 0
    assert up == 60 * 60


def test_failed_polls_count_down():
    samples = polls(4) + polls(3, start=T0 + timedelta(minutes=20), ok=False) \
        + polls(4, start=T0 + timedelta(minutes=35), uptime_at_start=86400 * 4)
    up, down, unchecked, _ = uptime.judge(samples, T0)
    # 15 -> 35 min: last answer to first answer after the failures.
    assert down == 20 * 60
    assert unchecked == 0


def test_reboot_counts_down_until_boot():
    before = polls(4)                                    # 0..15 min
    after = polls(3, start=T0 + timedelta(minutes=20), uptime_at_start=120)   # booted at 18 min
    up, down, unchecked, reboots = uptime.judge(before + after, T0)
    assert reboots == 1
    assert down == 3 * 60                                # 15 -> 18 min


def test_long_unpolled_gap_is_unchecked_not_down():
    first = polls(2)
    # Polled again 5 h later, just rebooted 10 min before: the gap can't be judged.
    later = polls(2, start=T0 + timedelta(hours=5), uptime_at_start=600)
    _, down, unchecked, _ = uptime.judge(first + later, T0)
    assert down == 0
    assert unchecked > 4 * 3600


def test_uptime_bridges_a_gap_in_polling():
    first = polls(2)
    later = polls(2, start=T0 + timedelta(hours=5), uptime_at_start=86400 * 3 + 5 * 3600)
    up, down, unchecked, reboots = uptime.judge(first + later, T0)
    assert down == 0 and unchecked == 0 and reboots == 0


def test_maintenance_is_left_out():
    samples = polls(4) + polls(3, start=T0 + timedelta(minutes=20), ok=False) \
        + polls(4, start=T0 + timedelta(minutes=35), uptime_at_start=86400 * 4)
    hole = (T0 + timedelta(minutes=15), T0 + timedelta(minutes=35))
    _, down, _, _ = uptime.judge(samples, T0, [hole])
    assert down == 0
