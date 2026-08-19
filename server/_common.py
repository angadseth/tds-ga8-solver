"""Shared helpers for GA8 handlers. Import from here; do not duplicate."""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone

MAX_SAFE_INT = 2 ** 53 - 1


def compact(obj) -> str:
    """Compact JSON exactly as JSON.stringify produces it (no spaces, non-ASCII raw)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def utf8_key(s: str) -> bytes:
    """Sort key giving UTF-8 byte order."""
    return s.encode("utf-8")


def is_safe_int(v, *, positive=False, non_negative=False) -> bool:
    if isinstance(v, bool) or not isinstance(v, int):
        return False
    if abs(v) > MAX_SAFE_INT:
        return False
    if positive and v <= 0:
        return False
    if non_negative and v < 0:
        return False
    return True


def is_finite_num(v) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


def in_unit(v) -> bool:
    return is_finite_num(v) and 0.0 <= v <= 1.0


_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?(Z|[+-]\d{2}:\d{2})$"
)


def parse_instant(s):
    """Parse YYYY-MM-DDTHH:mm:ss[.sss](Z|+HH:mm) -> aware datetime, or None."""
    if not isinstance(s, str):
        return None
    m = _TS.match(s)
    if not m:
        return None
    y, mo, d, h, mi, sec, frac, off = m.groups()
    y, mo, d, h, mi, sec = int(y), int(mo), int(d), int(h), int(mi), int(sec)
    if sec > 59 or mi > 59 or h > 23:
        return None
    ms = int((frac or "0").ljust(3, "0"))
    if off == "Z":
        delta = timedelta(0)
    else:
        sign = 1 if off[0] == "+" else -1
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 14 or om > 59 or (oh == 14 and om != 0):
            return None
        delta = sign * timedelta(hours=oh, minutes=om)
    try:
        naive = datetime(y, mo, d, h, mi, sec, ms * 1000)
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone.utc) - delta


def to_utc_string(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_WS = re.compile(r"\s+", re.UNICODE)


def canon_text(s: str) -> str:
    """NFKC, lowercase, trim, collapse Unicode whitespace to one ASCII space."""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = _WS.sub(" ", s).strip()
    return s


def sorted_unique_codes(codes):
    return sorted(set(codes), key=utf8_key)


def round12(x: float) -> float:
    return round(x, 12)


HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX8 = re.compile(r"^[0-9a-f]{8}$")


# --------------------------------------------------------------------------
# Tenant isolation.
#
# One deployment can serve many students if each one's stateful questions are
# kept apart: Q2 remembers a select's runId so the evaluate that follows can
# check lineage against it, and Q5 remembers a freeze so a differing replay can
# return 409.  Those live in process memory, so two students grading at the same
# time would otherwise collide on the same runId or freezeId.
#
# The tenant comes from the URL path (/ga8/<email>/...), is set per request, and
# is prefixed onto every store key.  A ContextVar rather than a global because
# FastAPI runs these handlers concurrently -- Starlette copies the request's
# context into the worker thread, so each request reads back its own value.
#
# Empty tenant == a personal single-student deployment, which is exactly the
# behaviour this service had before tenanting existed.
# --------------------------------------------------------------------------
from contextvars import ContextVar

TENANT: ContextVar = ContextVar("tenant", default="")


def tenant_key(key):
    """Namespace a store key to the student whose URL is being graded."""
    t = TENANT.get()
    return key if not t else (t, key)


# A shared deployment accumulates one entry per graded runId or freezeId across
# every student, and nothing ever deletes them.  Cap the total and drop the
# oldest first: a grading run only ever reads back keys it wrote seconds ago, so
# eviction this far down the queue cannot affect a run in progress.
STORE_MAX_ENTRIES = 50_000


def evict_oldest(store, limit=STORE_MAX_ENTRIES):
    if len(store) <= limit:
        return
    for key in list(store)[: len(store) - limit]:
        store.pop(key, None)
