"""Q1 — POST /build-corpus.

Deterministic, leakage-safe JSONL corpus builder.

Self-contained apart from the shared helpers in ``_common``; CRC32C lives here
(not in ``_common``) because it is only needed by this endpoint and the shared
file is being appended to concurrently by other handlers.
"""
import json
import re

from _common import (
    HEX8,
    canon_text,
    compact,
    in_unit,
    is_safe_int,
    parse_instant,
    sha256_hex,
    sorted_unique_codes,
    to_utc_string,
    utf8_key,
)

# --------------------------------------------------------------------------
# CRC32C (Castagnoli, poly 0x1EDC6F41 -> reflected 0x82F63B78)
# --------------------------------------------------------------------------
_CRC32C_POLY = 0x82F63B78

def _build_crc32c_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ (_CRC32C_POLY if crc & 1 else 0)
        table.append(crc & 0xFFFFFFFF)
    return tuple(table)

_CRC32C_TABLE = _build_crc32c_table()


def crc32c(data: bytes) -> int:
    """Reflected CRC-32C, init 0xFFFFFFFF, final xor 0xFFFFFFFF."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFFFFFF


def crc32c_hex(text: str) -> str:
    return f"{crc32c(text.encode('utf-8')):08x}"


# Self-test the polynomial at import time: the standard check vector.
assert crc32c_hex("123456789") == "e3069283", "CRC32C table is wrong"

# --------------------------------------------------------------------------
# Constants / small helpers
# --------------------------------------------------------------------------
SCHEMA_ID = "training-v1"
ROW_KEYS = ("id", "entity", "eventTime", "revision", "text")
_URI_RE = re.compile(r"^gs://[^/]+/.+$", re.DOTALL)
_DECIMAL_RE = re.compile(r"^[0-9]+$")
# Unicode letters/numbers, underscore excluded.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

BAD_REQUEST = (400, {"error": "INVALID_INPUT"})


def _reject_constant(_s):
    # JS JSON.parse rejects NaN/Infinity/-Infinity; Python's json accepts them.
    raise ValueError("non-JSON constant")


def _json_parse(line: str):
    return json.loads(line, parse_constant=_reject_constant)


def _same_json_value(a, b) -> bool:
    """Type-aware equality (1 != "1", True != 1) using canonical JSON text."""
    try:
        return compact(a) == compact(b)
    except (TypeError, ValueError):
        return a is b


def _is_decimal_string(v) -> bool:
    return isinstance(v, str) and _DECIMAL_RE.match(v) is not None


def _word_set(s: str):
    return set(_WORD_RE.findall(s.lower()))


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    if union == 0:
        return 1.0
    return len(a & b) / union


def _bucket(entity: str) -> int:
    return int(sha256_hex(entity.encode("utf-8"))[:2], 16) % 10


def _split_name(bucket: int) -> str:
    if bucket <= 5:
        return "train"
    if bucket <= 7:
        return "validation"
    return "test"


def _row_obj(row) -> dict:
    """Row dict in the exact serialization key order."""
    return {
        "id": row["id"],
        "entity": row["entity"],
        "eventTime": row["eventTime"],
        "revision": row["revision"],
        "text": row["text"],
    }


def _digest(rows) -> str:
    blob = b"".join(compact(_row_obj(r)).encode("utf-8") + b"\n" for r in rows)
    return sha256_hex(blob)


def _split_lines(content: str):
    return [ln[:-1] if ln.endswith("\r") else ln for ln in content.split("\n")]


# --------------------------------------------------------------------------
# Row / object validation
# --------------------------------------------------------------------------
def _parse_row(value):
    """Return a canonicalized row dict, or None when the shape is wrong."""
    if not isinstance(value, dict):
        return None
    if set(value.keys()) != set(ROW_KEYS):
        return None
    for key in ("id", "entity", "eventTime", "text"):
        if not isinstance(value[key], str):
            return None
    if not is_safe_int(value["revision"], non_negative=True):
        return None
    instant = parse_instant(value["eventTime"])
    if instant is None:
        return None
    return {
        "id": value["id"],
        "entity": canon_text(value["entity"]),
        "eventTime": to_utc_string(instant),
        "revision": value["revision"],
        "text": canon_text(value["text"]),
        "_instant": instant,
    }


def _check_object(obj):
    """Return (reason_codes, rows). rows is only meaningful when codes is empty."""
    codes = []
    src = obj if isinstance(obj, dict) else {}

    uri = src.get("uri")
    if not isinstance(uri, str) or not _URI_RE.match(uri):
        codes.append("URI_INVALID")

    gen = src.get("generation")
    fetched = src.get("fetchedGeneration")
    if not _is_decimal_string(gen) or not _is_decimal_string(fetched):
        codes.append("GENERATION_INVALID")
    if not _same_json_value(gen, fetched):
        codes.append("GENERATION_MISMATCH")

    content = src.get("content")
    crc = src.get("crc32c")
    crc_ok = isinstance(crc, str) and HEX8.match(crc) is not None
    if not crc_ok:
        codes.append("CRC32C_INVALID")
    elif isinstance(content, str) and crc32c_hex(content) != crc:
        # Checked only for string content and a syntactically valid CRC.
        codes.append("CRC32C_MISMATCH")

    if src.get("schemaId") != SCHEMA_ID:
        codes.append("SCHEMA_INVALID")

    rows = []
    if not isinstance(content, str):
        codes.append("SCHEMA_INVALID")
    else:
        raw_lines = [ln for ln in _split_lines(content) if ln.strip() != ""]
        if not raw_lines:
            codes.append("SCHEMA_INVALID")
        for line in raw_lines:
            try:
                parsed = _json_parse(line)
            except Exception:
                codes.append("JSONL_INVALID")
                continue
            row = _parse_row(parsed)
            if row is None:
                codes.append("SCHEMA_INVALID")
            else:
                rows.append(row)

    return sorted_unique_codes(codes), rows


def _policy_valid(policy):
    if not isinstance(policy, dict):
        return False, None, None, None
    lo = parse_instant(policy.get("minTime"))
    hi = parse_instant(policy.get("maxTime"))
    thr = policy.get("contaminationThreshold")
    if lo is None or hi is None or not in_unit(thr):
        return False, None, None, None
    if lo > hi:
        # An empty/reversed window is not a usable policy.
        return False, None, None, None
    return True, lo, hi, float(thr)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
_TRAFFIC = []


def handle(body: dict):
    """Return (http_status, response_body)."""
    if isinstance(body, dict) and "__dump__" in body:
        if body.get("__dump__") == "clear":
            _TRAFFIC.clear()
            return 200, {"cleared": True}
        return 200, {"n": len(_TRAFFIC), "log": _TRAFFIC}
    try:
        status, payload = _handle(body)
    except Exception:
        status, payload = BAD_REQUEST
    if len(_TRAFFIC) < 80:
        _TRAFFIC.append({"req": body, "status": status, "res": payload})
    return status, payload


def _handle(body: dict):
    if not isinstance(body, dict):
        return BAD_REQUEST
    if body.get("policy") is None:
        return BAD_REQUEST
    objects = body.get("objects")
    if not isinstance(objects, list):
        return BAD_REQUEST

    ok, min_time, max_time, threshold = _policy_valid(body.get("policy"))

    rejected_objects = []
    lineage = []
    pool = []

    for obj in objects:
        codes, rows = _check_object(obj)
        if codes:
            supplied = obj.get("uri") if isinstance(obj, dict) else None
            rejected_objects.append({
                "uri": supplied if isinstance(supplied, str) else None,
                "reasonCodes": codes,
            })
            continue
        lineage.append({
            "uri": obj["uri"],
            "generation": obj["generation"],
            "crc32c": obj["crc32c"],
            "schemaId": obj["schemaId"],
        })
        for row in rows:
            row["_order"] = len(pool)
            pool.append(row)

    # --- deduplication -----------------------------------------------------
    groups = {}
    for row in pool:
        key = compact([row["entity"], row["eventTime"], row["text"]])
        groups.setdefault(key, []).append(row)

    rejected_rows = []
    retained = []
    for key in groups:
        members = sorted(
            groups[key],
            key=lambda r: (-r["revision"], utf8_key(r["id"]), r["_order"]),
        )
        retained.append(members[0])
        for loser in members[1:]:
            rejected_rows.append({"id": loser["id"], "codes": ["DUPLICATE"]})
    retained.sort(key=lambda r: r["_order"])

    # --- policy / window ---------------------------------------------------
    survivors = []
    if not ok:
        for row in retained:
            rejected_rows.append({"id": row["id"], "codes": ["POLICY_INVALID"]})
    else:
        for row in retained:
            if row["_instant"] < min_time or row["_instant"] > max_time:
                rejected_rows.append({"id": row["id"], "codes": ["OUT_OF_WINDOW"]})
            else:
                survivors.append(row)

    # --- split assignment --------------------------------------------------
    splits = {"train": [], "validation": [], "test": []}
    for row in survivors:
        splits[_split_name(_bucket(row["entity"]))].append(row)

    # --- contamination -----------------------------------------------------
    if ok:
        train_sets = [_word_set(r["text"]) for r in splits["train"]]
        for name in ("validation", "test"):
            kept = []
            for row in splits[name]:
                words = _word_set(row["text"])
                contaminated = any(
                    _jaccard(words, tset) >= threshold for tset in train_sets
                )
                if contaminated:
                    rejected_rows.append(
                        {"id": row["id"], "codes": ["TRAIN_CONTAMINATION"]}
                    )
                else:
                    kept.append(row)
            splits[name] = kept

    # --- deterministic ordering / artifacts --------------------------------
    out_splits = {}
    digests = {}
    for name in ("train", "validation", "test"):
        rows = [_row_obj(r) for r in splits[name]]
        rows.sort(key=lambda r: (utf8_key(r["id"]), utf8_key(compact(r))))
        out_splits[name] = rows
        digests[name] = _digest(rows)

    rejected_objects.sort(
        key=lambda e: (
            0 if isinstance(e["uri"], str) else 1,
            utf8_key(e["uri"]) if isinstance(e["uri"], str) else b"",
            utf8_key(compact(e)),
        )
    )
    rejected_rows_out = [
        {"id": e["id"], "reasonCodes": sorted_unique_codes(e["codes"])}
        for e in rejected_rows
    ]
    rejected_rows_out.sort(key=lambda e: (utf8_key(e["id"]), utf8_key(compact(e))))
    lineage.sort(key=lambda e: (utf8_key(e["uri"]), utf8_key(compact(e))))

    return 200, {
        "splits": out_splits,
        "rejectedObjects": rejected_objects,
        "rejectedRows": rejected_rows_out,
        "digests": digests,
        "lineage": lineage,
    }
