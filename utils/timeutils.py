"""
utils/timeutils.py — CANONICAL time helpers untuk QF_BIAS.

⚠ FILE INI MENGGANTIKAN SEMUA versi timeutils dari Sesi 1/2/3.
   Sesi-sesi menulis 3 versi berbeda yang konflik; ini superset gabungan.

Standar:
  - Semua timestamp internal = datetime UTC-aware.
  - WIB (UTC+7, Asia/Jakarta) hanya untuk display & perbandingan "now".
  - Pakai zoneinfo (stdlib, Python 3.9+) — TIDAK pakai pytz (hindari dependency).
  - String tz diambil dari config.TIMEZONE_WIB (single source of truth).

Fungsi yang diekspor (dipakai lintas modul):
  Waktu sekarang : now_utc, now_wib
  Konversi       : to_utc, to_wib (alias utc_to_wib), wib_to_utc
  Format         : fmt_iso_utc, fmt_wib_display (alias fmt_utc, fmt_wib)
  Parsing        : parse_iso_utc (alias parse_ts_utc)
  Event/umur     : event_status, minutes_until, minutes_since, age_minutes, countdown_str
  COT helpers    : last_cot_tuesday, cot_release_friday, days_since_cot_snapshot
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Literal

from config import TIMEZONE_WIB

_UTC = timezone.utc
_WIB = ZoneInfo(TIMEZONE_WIB)

_FMT_ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"
_FMT_DISPLAY = "%Y-%m-%d %H:%M"

EventStatus = Literal["upcoming", "released"]


# ---------------------------------------------------------------------------
# WAKTU SEKARANG
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    """Datetime sekarang, UTC-aware."""
    return datetime.now(tz=_UTC)


def now_wib() -> datetime:
    """Datetime sekarang dalam WIB (UTC+7), aware."""
    return datetime.now(tz=_UTC).astimezone(_WIB)


# ---------------------------------------------------------------------------
# KONVERSI
# ---------------------------------------------------------------------------

def _ensure_utc(dt: datetime) -> datetime:
    """Internal: pastikan dt UTC-aware. Naive → diasumsikan UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def to_utc(dt: datetime) -> datetime:
    """Konversi datetime aware/naive → UTC-aware. Naive diasumsikan UTC."""
    return _ensure_utc(dt)


def to_wib(dt: datetime) -> datetime:
    """Konversi datetime → WIB-aware. Naive diasumsikan UTC."""
    return _ensure_utc(dt).astimezone(_WIB)


# Alias kompatibilitas (Sesi 3 memakai nama ini)
utc_to_wib = to_wib


def wib_to_utc(dt: datetime) -> datetime:
    """Konversi datetime WIB (aware, atau naive diasumsikan WIB) → UTC-aware."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_WIB)
    return dt.astimezone(_UTC)


# ---------------------------------------------------------------------------
# FORMAT → STRING
# ---------------------------------------------------------------------------

def fmt_iso_utc(dt: datetime) -> str:
    """ISO-8601 UTC dgn suffix Z: '2026-06-01T07:00:00Z'. Naive diasumsikan UTC."""
    return _ensure_utc(dt).strftime(_FMT_ISO_UTC)


def fmt_wib_display(dt: datetime) -> str:
    """Display WIB: '2026-06-01 14:00'. Naive diasumsikan UTC lalu dikonversi."""
    return to_wib(dt).strftime(_FMT_DISPLAY)


# Alias kompatibilitas (Sesi 1 memakai nama ini)
fmt_utc = lambda dt: _ensure_utc(dt).strftime(_FMT_DISPLAY)  # noqa: E731
fmt_wib = fmt_wib_display


# ---------------------------------------------------------------------------
# PARSING
# ---------------------------------------------------------------------------

def parse_iso_utc(ts: str | datetime) -> datetime:
    """Parse timestamp → datetime UTC-aware.

    Menerima:
      - '2026-06-01T07:00:00Z'
      - '2026-06-01T07:00:00+00:00' / dengan offset lain
      - '2026-06-01 07:00:00' (naive → diasumsikan UTC)
      - datetime (naive → UTC; aware → dikonversi)
    Raises ValueError kalau string tidak bisa diparse.
    """
    if isinstance(ts, datetime):
        return _ensure_utc(ts)

    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"Tidak bisa parse timestamp: {ts!r}") from exc
    return _ensure_utc(dt)


# Alias kompatibilitas (Sesi 1 memakai nama ini)
parse_ts_utc = parse_iso_utc


# ---------------------------------------------------------------------------
# EVENT STATUS & UMUR
# ---------------------------------------------------------------------------

def event_status(ts_utc: str | datetime, now: Optional[datetime] = None) -> EventStatus:
    """'upcoming' kalau event > now, else 'released'. Terima str atau datetime."""
    now = _ensure_utc(now) if now is not None else now_utc()
    return "upcoming" if parse_iso_utc(ts_utc) > now else "released"


def minutes_until(ts_utc: str | datetime, now: Optional[datetime] = None) -> float:
    """Menit sampai ts_utc. Positif = belum terjadi; negatif = sudah lewat."""
    now = _ensure_utc(now) if now is not None else now_utc()
    return (parse_iso_utc(ts_utc) - now).total_seconds() / 60.0


def minutes_since(ts_utc: str | datetime, now: Optional[datetime] = None) -> float:
    """Menit sejak ts_utc. Positif = sudah lewat."""
    return -minutes_until(ts_utc, now=now)


# Alias semantik (Sesi 3 / news_overlay memakai age_minutes)
def age_minutes(ts_utc: str | datetime, now: Optional[datetime] = None) -> float:
    """Umur (menit) sejak ts_utc. Positif = di masa lalu. = minutes_since."""
    return minutes_since(ts_utc, now=now)


def countdown_str(ts_utc: str | datetime, now: Optional[datetime] = None) -> str:
    """Countdown human-readable: 'dalam 2j 15m' / '2j 15m lalu' / 'baru saja'."""
    mins = minutes_until(ts_utc, now=now)
    if abs(mins) < 1:
        return "baru saja"
    total = int(abs(mins))
    h, m = divmod(total, 60)
    parts = []
    if h:
        parts.append(f"{h}j")
    if m:
        parts.append(f"{m}m")
    dur = " ".join(parts) or "0m"
    return f"dalam {dur}" if mins > 0 else f"{dur} lalu"


# ---------------------------------------------------------------------------
# COT DATE HELPERS (dipakai collectors/cot.py & engine/freshness.py)
# ---------------------------------------------------------------------------

def last_cot_tuesday(reference: datetime | None = None) -> datetime:
    """Tuesday COT-snapshot terakhir, midnight UTC.

    CFTC ambil data per Selasa-close, rilis Jumat ~15:30 ET.
    Kalau hari ini Selasa → kembalikan hari ini.
    """
    ref = _ensure_utc(reference) if reference is not None else now_utc()
    days_since_tue = (ref.weekday() - 1) % 7   # Mon=0, Tue=1
    tuesday = ref - timedelta(days=days_since_tue)
    return tuesday.replace(hour=0, minute=0, second=0, microsecond=0)


def cot_release_friday(tuesday: datetime) -> datetime:
    """Jumat rilis (Selasa + 3 hari) untuk snapshot tertentu."""
    return tuesday + timedelta(days=3)


def days_since_cot_snapshot(snapshot_tuesday: datetime | None = None) -> int:
    """Integer hari antara snapshot Selasa dan hari ini (UTC). Dipakai freshness decay."""
    snap = snapshot_tuesday if snapshot_tuesday is not None else last_cot_tuesday()
    snap = _ensure_utc(snap)
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, (today - snap).days)
