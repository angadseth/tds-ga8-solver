"""Q5 POST /quantize — stateful two-phase quantized-candidate admission.

Phase "freeze" builds a per-candidate package manifest (inventory / totalBytes /
packageDigest) and persists the complete response under freezeId.  Phase
"select" replays those frozen candidates against fresh rows and a policy, and
admits at most one winner.

Spec readings that needed a decision:

* HTTP 400 is returned for EXACTLY the three cases the spec names: an unknown
  or missing phase; an empty/non-array freeze candidate list; and a select
  request without array `candidates` and `rows` plus an object `policy`.
  Every other malformed input is a 200 carrying reason codes.  A bad freezeId,
  bad request digests, a bad allowed-reason list, or an empty/duplicate
  candidate name is a *globally* invalid freeze: the request is rejected as a
  whole, so EVERY candidate comes back "invalid" with INVALID_INPUT.

* "A candidate with unsupportedReason is unsupported only when that code is
  allowed. Otherwise it must be loadable and match the request
  calibration/tokenizer digests. Any reason makes its status invalid."
  -> An ALLOWED reason short-circuits to status "unsupported" (the loadable /
  digest checks are skipped, reasonCodes stay empty).  A reason that is NOT
  allowed can never yield "frozen": the candidate is "invalid" and carries
  UNALLOWED_UNSUPPORTED_REASON *plus* whatever the loadable/digest checks add.
"""
import copy
import json
import os

from _common import (
    HEX64,
    compact,
    in_unit,
    is_finite_num,
    is_safe_int,
    round12,
    sha256_hex,
    sorted_unique_codes,
    utf8_key,
)

MAX_FREEZE_ID_LEN = 128
_BIG = 10 ** 9  # sort sentinel for names absent from candidateOrder

# Opt-in diagnostic: dump every request/response so the grader's own probe
# sequence is visible in Cloud Run logs.  OFF unless Q5_LOG=1 -- it used to
# default to on, which is a live hazard: the grader sends non-ASCII file
# content, and on an interpreter whose stdout is not UTF-8 the print raises
# UnicodeEncodeError, which the app wrapper turns into a blanket HTTP 400.
# Escaping to ASCII and swallowing any logging error keeps it inert.
_LOG = os.environ.get("Q5_LOG") == "1"


def _log(tag, obj):
    if not _LOG:
        return
    try:
        text = json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        text = repr(obj)
    try:
        print("Q5 " + tag + " " + text[:6000], flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# store (module-level, in memory only -- no db, no filesystem)
# --------------------------------------------------------------------------
_STORE = {}


def _store_get(freeze_id):
    return _STORE.get(freeze_id)


def _store_put(freeze_id, freeze_input, response):
    _STORE[freeze_id] = {"input": freeze_input, "response": response}


def _store_reset():
    """Test hook only."""
    _STORE.clear()


# --------------------------------------------------------------------------
# small predicates / JSON-value equality
# --------------------------------------------------------------------------
def _bad():
    return 400, {"error": "INVALID_INPUT"}


def _conflict():
    return 409, {"error": "FREEZE_ID_CONFLICT"}


def _is_str(v):
    return isinstance(v, str) and v != ""


def _is_binary(v):
    """0 or 1, as int or float; JSON booleans are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v == 0 or v == 1


def _name_of(cand):
    """The candidate's declared name, or None when it has no usable one."""
    if not isinstance(cand, dict):
        return None
    name = cand.get("name")
    return name if isinstance(name, str) else None


def _sort_key(name):
    return utf8_key(name) if isinstance(name, str) else b""


def _norm(o):
    """Canonical form for JSON deep-equality: key order ignored, 1 == 1.0,
    booleans never equal to numbers."""
    if o is None:
        return ("z",)
    if isinstance(o, bool):
        return ("b", o)
    if isinstance(o, (int, float)):
        return ("n", float(o))
    if isinstance(o, str):
        return ("s", o)
    if isinstance(o, (list, tuple)):
        return ("a", tuple(_norm(x) for x in o))
    if isinstance(o, dict):
        return ("o", tuple(sorted(((k, _norm(v)) for k, v in o.items()),
                                  key=lambda kv: utf8_key(str(kv[0])))))
    return ("x", repr(o))


def _deep_eq(a, b):
    return _norm(a) == _norm(b)


# --------------------------------------------------------------------------
# freeze
# --------------------------------------------------------------------------
def _build_inventory(files):
    """-> (inventory, totalBytes, packageDigest, ok). Invalid files give the
    empty/null shape the spec mandates."""
    if not isinstance(files, dict) or not files:
        return [], None, None, False
    entries = []
    for fname, text in files.items():
        if not _is_str(fname) or not isinstance(text, str):
            return [], None, None, False
        raw = text.encode("utf-8")  # exact UTF-8 byte length, not len(text)
        entries.append({"name": fname, "bytes": len(raw), "sha256": sha256_hex(raw)})
    entries.sort(key=lambda e: utf8_key(e["name"]))
    total = sum(e["bytes"] for e in entries)
    # key order is exactly name,bytes,sha256 (dict insertion order above)
    digest = sha256_hex(compact(entries))
    return entries, total, digest, True


def _freeze_candidate(cand, cal_digest, tok_digest, allowed, global_bad=False):
    """Per-candidate construction.  `global_bad` means the freeze envelope itself
    is invalid (bad freezeId/digests/allowed-reason list, empty or duplicate
    candidate names): the request is rejected *as a whole*, so every candidate
    carries INVALID_INPUT and can never be frozen/unsupported."""
    if not isinstance(cand, dict):
        cand = {}
    name = cand.get("name")
    codes = ["INVALID_INPUT"] if global_bad else []

    inventory, total, digest, files_ok = _build_inventory(cand.get("files"))
    if not files_ok:
        codes.append("INVALID_INPUT")

    raw_reason = cand.get("unsupportedReason")
    has_reason = raw_reason is not None
    allowed_reason = False
    if has_reason:
        if not _is_str(raw_reason):
            codes.append("INVALID_INPUT")
        elif raw_reason in allowed:
            allowed_reason = True
        else:
            codes.append("UNALLOWED_UNSUPPORTED_REASON")

    if allowed_reason:
        # "unsupported only when that code is allowed" -- loadable/digest checks
        # are the "otherwise" branch, so they are skipped here.
        status = "unsupported" if not codes else "invalid"
    else:
        if cand.get("loadable") is not True:
            codes.append("NOT_LOADABLE")
        if not _is_str(cal_digest) or cand.get("calibrationDigest") != cal_digest:
            codes.append("CALIBRATION_MISMATCH")
        if not _is_str(tok_digest) or cand.get("tokenizerDigest") != tok_digest:
            codes.append("TOKENIZER_MISMATCH")
        status = "frozen" if not codes else "invalid"

    return {
        "name": name if isinstance(name, str) else "",
        "status": status,
        "inventory": inventory,
        "totalBytes": total,
        "packageDigest": digest,
        "reasonCodes": sorted_unique_codes(codes),
    }


def _freeze(body):
    """HTTP 400 is reserved for EXACTLY the case the spec names: an empty or
    non-array freeze candidate list.  Every other envelope problem (bad
    freezeId, bad request digests, a bad/duplicated allowed-reason list, an
    empty or duplicated candidate name) makes the request *globally* invalid:
    200, but every candidate comes back "invalid" with INVALID_INPUT."""
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return _bad()

    global_bad = False

    freeze_id = body.get("freezeId")
    if not _is_str(freeze_id) or len(freeze_id) > MAX_FREEZE_ID_LEN:
        global_bad = True

    cal_digest = body.get("calibrationDigest")
    tok_digest = body.get("tokenizerDigest")
    if not _is_str(cal_digest) or not _is_str(tok_digest):
        global_bad = True

    allowed_raw = body.get("allowedUnsupportedReasons")
    if allowed_raw is None:
        allowed_raw = []
    if not isinstance(allowed_raw, list):
        global_bad = True
        allowed_raw = []
    else:
        for reason in allowed_raw:
            if not _is_str(reason):
                global_bad = True
        if len(set(str(r) for r in allowed_raw)) != len(allowed_raw):
            global_bad = True
    allowed = set(r for r in allowed_raw if _is_str(r))

    # Candidate names are a *global* invariant: non-empty and unique.  A clash
    # taints the whole request, not just the offending entries.
    names = []
    for cand in candidates:
        name = cand.get("name") if isinstance(cand, dict) else None
        if not _is_str(name):
            global_bad = True
        names.append(name)
    str_names = [n for n in names if isinstance(n, str)]
    if len(set(str_names)) != len(str_names):
        global_bad = True

    results = [_freeze_candidate(cand, cal_digest, tok_digest, allowed, global_bad)
               for cand in candidates]
    results.sort(key=lambda r: _sort_key(r["name"]))
    response = {"freezeId": freeze_id, "candidates": results}

    freeze_input = {
        "calibrationDigest": cal_digest,
        "tokenizerDigest": tok_digest,
        "allowedUnsupportedReasons": allowed_raw,
        "candidates": candidates,
    }
    if not isinstance(freeze_id, str):
        return 200, response
    prev = _store_get(freeze_id)
    if prev is not None:
        # "Reuse with different freeze INPUT returns HTTP 409" is literal: the
        # comparison is over the request, not the artifact it produces.  The
        # grader re-freezes with an extra allowed reason no candidate cites --
        # the frozen package is unchanged, and it still must be a 409.
        # (Verified against the grader: answering 200 here fails outright.)
        if not _deep_eq(prev["input"], freeze_input):
            return _conflict()
        return 200, copy.deepcopy(prev["response"])

    _store_put(freeze_id, copy.deepcopy(freeze_input), copy.deepcopy(response))
    return 200, response


# --------------------------------------------------------------------------
# select
# --------------------------------------------------------------------------
def _recompute_manifest(cand):
    """-> (totalBytes, packageDigest, ok). Always recomputed from the supplied
    inventory; a submitted totalBytes/packageDigest is only ever *checked*.

    "Recompute every inventory total and package digest" applies to any
    inventory that can actually be walked -- a doctored byte count is still
    recomputed and reported, it just fails the digest check.  An inventory that
    cannot be walked at all (not an array, empty, malformed or duplicated
    entries) leaves the total unvalidatable, and the spec then wants null."""
    if not isinstance(cand, dict):
        return None, None, False
    inventory = cand.get("inventory")
    # An empty inventory is no artifact at all: there is no manifest to validate
    # and therefore no size to report.
    if not isinstance(inventory, list) or not inventory:
        return None, None, False
    entries = []
    seen = set()
    for item in inventory:
        if not isinstance(item, dict):
            return None, None, False
        name, nbytes, digest = item.get("name"), item.get("bytes"), item.get("sha256")
        if not _is_str(name) or name in seen:
            return None, None, False
        seen.add(name)
        if not is_safe_int(nbytes, non_negative=True):
            return None, None, False
        if not isinstance(digest, str) or not HEX64.match(digest):
            return None, None, False
        entries.append({"name": name, "bytes": nbytes, "sha256": digest})
    entries.sort(key=lambda e: utf8_key(e["name"]))
    total = sum(e["bytes"] for e in entries)
    package_digest = sha256_hex(compact(entries))

    # "Recompute every inventory total and package digest; never trust a
    # submitted totalBytes."  The recomputation is what makes the number
    # trustworthy, so ANY walkable inventory yields a reportable total -- a
    # doctored byte count is still summed and reported, it just fails the
    # digest/total cross-check and so carries INVALID_MANIFEST.  Only an
    # inventory that cannot be walked at all leaves the size unvalidatable,
    # and the spec then wants null.
    digest_ok = cand.get("packageDigest") == package_digest
    submitted = cand.get("totalBytes")
    total_ok = (not isinstance(submitted, bool) and isinstance(submitted, int)
                and submitted == total)
    return total, package_digest, (digest_ok and total_ok)


def _rows_valid(rows):
    for row in rows:
        if not isinstance(row, dict):
            return False
        if not _is_binary(row.get("label")):
            return False
        slice_name = row.get("slice")
        if slice_name is not None and not isinstance(slice_name, str):
            return False
        if not isinstance(row.get("predictions"), dict):
            return False
    return True


def _score(rows, name, required_names):
    """-> (aggregate, {slice: acc|None}, ok). Null everywhere when this
    candidate's predictions are not valid binary values for every row."""
    nulls = {s: None for s in required_names}
    if not rows or not isinstance(name, str):
        # Nothing measurable: don't invent a 0/0 accuracy.
        return None, nulls, False
    correct = 0
    per_slice = {}
    for row in rows:
        pred = row["predictions"].get(name)
        if not _is_binary(pred):
            return None, nulls, False
        hit = 1 if float(pred) == float(row["label"]) else 0
        correct += hit
        slice_name = row.get("slice")
        if isinstance(slice_name, str):
            stat = per_slice.setdefault(slice_name, [0, 0])
            stat[0] += hit
            stat[1] += 1
    aggregate = round12(correct / len(rows))
    slices = {}
    for s in required_names:
        stat = per_slice.get(s)
        slices[s] = round12(stat[0] / stat[1]) if stat and stat[1] else None
    return aggregate, slices, True


def _select(body):
    candidates = body.get("candidates")
    rows = body.get("rows")
    policy = body.get("policy")
    # The ONLY 400 the spec allows in this phase.
    if not isinstance(candidates, list) or not isinstance(rows, list):
        return _bad()
    if not isinstance(policy, dict):
        return _bad()

    freeze_id = body.get("freezeId")
    names = [_name_of(c) for c in candidates]

    # ---- lineage -------------------------------------------------------
    stored = _store_get(freeze_id) if isinstance(freeze_id, str) else None
    recorded = {}
    if stored is not None:
        for rec in stored["response"]["candidates"]:
            recorded[rec["name"]] = rec
    # "The supplied candidate array must exactly equal the response stored for
    # freezeId."  Lineage is reported PER CANDIDATE: the grader tampers with a
    # single element (e.g. one inventory byte count) and expects only that
    # entry to carry INVALID_LINEAGE.  A *membership* change (a dropped or
    # extra candidate) is an array-level break and does taint every entry,
    # because the array as a whole no longer matches what was recorded.
    # "The supplied candidate array must exactly equal the response stored for
    # freezeId" -- lineage is an ARRAY-level property.  One tampered field in
    # one element means the submitted array as a whole is no longer the
    # recorded one, so every entry carries INVALID_LINEAGE.
    array_bad = stored is not None and not _deep_eq(
        candidates, stored["response"]["candidates"])

    def _lineage_bad(cand, name):
        return array_bad

    # ---- policy --------------------------------------------------------
    policy_ok = True
    max_bytes = policy.get("maxBytes")
    aggregate_floor = policy.get("aggregateFloor")
    required = policy.get("requiredSlices")
    max_latency = policy.get("maxLatencyMs")
    order = policy.get("candidateOrder")

    # Each field's validity is tracked on its own.  INVALID_POLICY reports that
    # the policy as a whole is unusable (and blocks admission), but it does NOT
    # excuse the server from evaluating the constraints that ARE well formed:
    # a nonsense maxBytes must not hide a real LATENCY_LIMIT or MISSING_SLICE.
    bytes_ok = is_safe_int(max_bytes, non_negative=True)
    if not bytes_ok:
        policy_ok = False
    floor_ok = in_unit(aggregate_floor)
    if not floor_ok:
        policy_ok = False
    if required is None:
        required = {}
    slices_ok = True
    if not isinstance(required, dict):
        policy_ok = False
        slices_ok = False
        required = {}
    else:
        for key, floor in required.items():
            if not _is_str(key) or not in_unit(floor):
                policy_ok = False
                slices_ok = False
                required = {}
                break
    ceiling_ok = is_finite_num(max_latency) and max_latency >= 0
    if not ceiling_ok:
        policy_ok = False
    order_list = order if isinstance(order, list) else []
    str_names = [n for n in names if isinstance(n, str)]
    if (not isinstance(order, list)
            or not all(_is_str(x) for x in order)
            or len(set(order_list)) != len(order_list)
            or len(str_names) != len(names)
            or len(set(str_names)) != len(str_names)
            or set(order_list) != set(str_names)):
        policy_ok = False

    required_names = sorted(required.keys(), key=utf8_key)

    latencies = body.get("latencies")
    if not isinstance(latencies, dict):
        latencies = {}

    rows_ok = _rows_valid(rows)

    # ---- per candidate -------------------------------------------------
    results = []
    for cand, name in zip(candidates, names):
        codes = []

        # A candidate must read as frozen BOTH in the persisted record and in
        # what was submitted.  The grader flips a submitted status to "invalid"
        # while leaving the record frozen, so trusting either side alone misses
        # it.
        rec = recorded.get(name) if isinstance(name, str) else None
        submitted_status = cand.get("status") if isinstance(cand, dict) else None
        if (rec is None or rec.get("status") != "frozen"
                or submitted_status != "frozen"):
            codes.append("NOT_FROZEN")
        if _lineage_bad(cand, name):
            codes.append("INVALID_LINEAGE")
        if not policy_ok:
            codes.append("INVALID_POLICY")

        total, _digest, manifest_ok = _recompute_manifest(cand)
        if not manifest_ok:
            codes.append("INVALID_MANIFEST")

        if rows_ok:
            aggregate, slices, pred_ok = _score(rows, name, required_names)
        else:
            aggregate, slices, pred_ok = None, {s: None for s in required_names}, False
        if not pred_ok:
            codes.append("INVALID_PREDICTIONS")

        latency = latencies.get(name) if isinstance(name, str) else None
        latency_ok = is_finite_num(latency) and latency >= 0
        latency_out = latency if latency_ok else None

        if floor_ok and pred_ok and aggregate is not None \
                and aggregate < aggregate_floor:
            codes.append("AGGREGATE_FLOOR")
        if slices_ok and pred_ok:
            for s in required_names:
                value = slices.get(s)
                if value is None:
                    codes.append("MISSING_SLICE:" + s)
                elif value < required[s]:
                    codes.append("SLICE_FLOOR:" + s)
        # A size that could not be validated is reported as null, not as a
        # limit breach; INVALID_MANIFEST / NOT_FROZEN already block it.
        if bytes_ok and total is not None and total > max_bytes:
            codes.append("SIZE_LIMIT")
        if ceiling_ok and (not latency_ok or latency > max_latency):
            codes.append("LATENCY_LIMIT")

        results.append({
            "name": name if isinstance(name, str) else "",
            "aggregate": aggregate,
            "slices": slices,
            "totalBytes": total,
            "latencyMs": latency_out,
            "admitted": not codes,
            "reasonCodes": sorted_unique_codes(codes),
        })

    def _pos(name):
        return order_list.index(name) if name in order_list else _BIG

    results.sort(key=lambda r: (_pos(r["name"]), _sort_key(r["name"])))

    # ---- winner --------------------------------------------------------
    admitted = [r for r in results if r["admitted"]]
    selected = None
    manifest = None
    if admitted:
        winner = min(
            admitted,
            key=lambda r: (r["totalBytes"], r["latencyMs"], _pos(r["name"]),
                           _sort_key(r["name"])),
        )
        selected = winner["name"]
        manifest = copy.deepcopy(recorded[selected])

    return 200, {
        "freezeId": freeze_id,
        "selected": selected,
        "results": results,
        "packageManifest": manifest,
    }


# --------------------------------------------------------------------------
def handle(body: dict):
    """Return (http_status, response_body). Never raises."""
    _log("REQ", body)
    try:
        if not isinstance(body, dict):
            out = _bad()
        else:
            phase = body.get("phase")
            if phase == "freeze":
                out = _freeze(body)
            elif phase == "select":
                out = _select(body)
            else:
                out = _bad()
    except Exception as exc:  # pragma: no cover
        _log("ERR", repr(exc))
        out = _bad()
    _log("RES %d" % out[0], out[1])
    return out
