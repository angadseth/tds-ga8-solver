"""Q2 - POST /bqml: leakage-safe BigQuery ML experiment gate (stateful).

Two phases:
  select   -> picks a trial + freezes the point-in-time-correct dataset, and the
              complete response is persisted under runId (idempotent replay,
              409 on reuse with different selection input).
  evaluate -> scores frozen-lineage test rows against aggregate/slice floors.
"""
import json as _json
import os as _os
import sys as _sys

from _common import (
    HEX64,
    compact,
    in_unit,
    is_finite_num,
    is_safe_int,
    parse_instant,
    round12,
    sha256_hex,
    sorted_unique_codes,
    tenant_key,
    evict_oldest as _evict,
    utf8_key,
)

# --------------------------------------------------------------------------
# Store: single-process in-memory dict behind a tiny get/put facade so the
# backing implementation can be swapped (Redis/Firestore) without touching the
# handler.  No DB, no filesystem.
# --------------------------------------------------------------------------
_MEM = {}


class _MemoryStore:
    """Keys are namespaced per student, so two people grading at the same time
    cannot collide on the same runId.  See tenant_key in _common."""

    def get(self, key):
        return _MEM.get(tenant_key(key))

    def put(self, key, value):
        _MEM[tenant_key(key)] = value
        _evict(_MEM)


_store = _MemoryStore()


def _reset_store():
    """Test hook only."""
    _MEM.clear()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _canon(o):
    """Recursively order dict keys by UTF-8 bytes so a fingerprint is stable."""
    if isinstance(o, dict):
        return {k: _canon(o[k]) for k in sorted(o.keys(), key=utf8_key)}
    if isinstance(o, list):
        return [_canon(x) for x in o]
    return o


def _selection_fingerprint(body):
    """Canonical compact JSON of the *selection-relevant* request fields.

    runId is the store key itself and `phase` is fixed at "select" here, so
    neither is part of the identity.  Everything the selection algorithm reads
    -- forbiddenFeatures, numTrialsLimit, rows, trials -- is included verbatim
    (raw, pre-normalisation) so that two requests differing only in, say, a
    forbidden feature still conflict even when they'd produce the same output.
    Unknown extra fields are ignored: they cannot change the response.
    List order is significant; a reordered rows array is different input.
    """
    return compact(
        _canon(
            {
                "forbiddenFeatures": body.get("forbiddenFeatures"),
                "numTrialsLimit": body.get("numTrialsLimit"),
                "rows": body.get("rows"),
                "trials": body.get("trials"),
            }
        )
    )


def _valid_run_id(v):
    return isinstance(v, str) and 0 < len(v) <= 128


def _bad(status, code):
    return status, {"error": code}


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------
_PROBE = [0]


# Opt-in: a shared deployment serves every student's grading run, and printing
# each one costs log volume for no benefit.  Set Q2_LOG=1 when debugging.
_LOG_ON = _os.environ.get("Q2_LOG") == "1"


def _log(body, status, resp):
    if not _LOG_ON:
        return
    _PROBE[0] += 1
    try:
        line = "PROBE#%d %s" % (
            _PROBE[0],
            _json.dumps({"req": body, "status": status, "resp": resp},
                        default=str)[:6000],
        )
    except Exception:
        line = "PROBE#%d <unserialisable>" % _PROBE[0]
    print(line, flush=True)
    _sys.stdout.flush()


def handle(body):
    out = _handle(body)
    _log(body, out[0], out[1])
    return out


def _handle(body):
    """Return (http_status, response_body).  Never raises."""
    try:
        if not isinstance(body, dict):
            return _bad(400, "INVALID_INPUT")
        phase = body.get("phase")
        if phase == "select":
            return _select(body)
        if phase == "evaluate":
            return _evaluate(body)
        return _bad(400, "INVALID_INPUT")
    except Exception:
        return _bad(400, "INVALID_INPUT")


# --------------------------------------------------------------------------
# phase: select
# --------------------------------------------------------------------------
def _select(body):
    run_id = body.get("runId")
    run_key = run_id if _valid_run_id(run_id) else None

    if run_key is not None:
        stored = _store.get(run_key)
        if stored is not None:
            if stored["fingerprint"] == _selection_fingerprint(body):
                # Identical replay: return the persisted object unchanged.
                return 200, stored["response"]
            return _bad(409, "RUN_ID_CONFLICT")

    response = _compute_select(body, run_key)

    # Persistence note: the spec says "Persist the complete response under
    # runId" without qualification, and its only 400 path (unknown/missing
    # phase) never reaches here.  So a 200 response *does* reserve the runId
    # even when it carries INVALID_INPUT.  Q5 explicitly exempts its rejected
    # (HTTP 400) freeze requests; Q2 is silent, and its 400s never get this far
    # anyway, so the two readings agree.  A runId we cannot key on (missing /
    # non-string / empty / >128 chars) is simply not stored.
    if run_key is not None:
        _store.put(
            run_key,
            {"fingerprint": _selection_fingerprint(body), "response": response},
        )
    return 200, response


def _select_response(run_id, trial_id, train_ids, eval_ids, feature_names,
                     digest, codes):
    return {
        "runId": run_id,
        "selectedTrialId": trial_id,
        "trainRowIds": train_ids,
        "evalRowIds": eval_ids,
        "featureNames": feature_names,
        "datasetDigest": digest,
        "reasonCodes": sorted_unique_codes(codes),
    }


def _compute_select(body, run_key):
    run_id = body.get("runId") if isinstance(body.get("runId"), str) else None
    codes = []
    malformed = run_key is None  # runId itself must be a 1..128 char string

    # ---- forbiddenFeatures (absent -> none forbidden) ----
    forbidden_raw = body.get("forbiddenFeatures")
    forbidden = set()
    if forbidden_raw is None:
        pass
    elif isinstance(forbidden_raw, list) and all(
        isinstance(f, str) for f in forbidden_raw
    ):
        forbidden = set(forbidden_raw)
    else:
        malformed = True

    # ---- numTrialsLimit (absent -> no limit) ----
    limit_raw = body.get("numTrialsLimit")
    limit = None
    limit_ok = True
    if limit_raw is None:
        pass
    elif is_safe_int(limit_raw, positive=True):
        limit = limit_raw
    else:
        malformed = True
        limit_ok = False

    # ---- rows ----
    rows_raw = body.get("rows")
    rows_ok = isinstance(rows_raw, list) and len(rows_raw) > 0
    parsed_rows = []
    if not rows_ok:
        malformed = True
    else:
        seen_ids = set()
        for r in rows_raw:
            pr = _parse_row(r)
            if pr is None or pr["id"] in seen_ids:
                malformed = True
                rows_ok = False
                break
            seen_ids.add(pr["id"])
            parsed_rows.append(pr)

    # ---- trials (absent -> empty) ----
    trials_raw = body.get("trials")
    if trials_raw is None:
        trials_raw = []
    trials_ok = isinstance(trials_raw, list)
    parsed_trials = []
    if not trials_ok:
        malformed = True
    else:
        seen_tids = set()
        for t in trials_raw:
            pt = _parse_trial(t)
            if pt is None or pt["trialId"] in seen_tids:
                malformed = True
                trials_ok = False
                break
            seen_tids.add(pt["trialId"])
            parsed_trials.append(pt)

    if malformed:
        codes.append("INVALID_INPUT")

    # Independent gates: each check still fires when its own inputs are sound.
    over_limit = trials_ok and limit_ok and limit is not None and len(trials_raw) > limit
    if over_limit:
        codes.append("TRIAL_LIMIT_EXCEEDED")

    selected = None
    if trials_ok:
        eligible = [
            t for t in parsed_trials
            if t["status"] == "SUCCEEDED" and is_finite_num(t["evalMetric"])
        ]
        if not eligible:
            codes.append("NO_SUCCESSFUL_TRIAL")
        else:
            # maximise evalMetric, break exact ties with the smallest trialId
            best = eligible[0]
            for t in eligible[1:]:
                if t["evalMetric"] > best["evalMetric"] or (
                    t["evalMetric"] == best["evalMetric"]
                    and t["trialId"] < best["trialId"]
                ):
                    best = t
            selected = best["trialId"]

    if malformed:
        # A malformed selection also returns a null datasetDigest.
        return _select_response(run_id, None, [], [], [], None, codes)

    train_ids, eval_ids, feature_names = _build_dataset(parsed_rows, forbidden)
    digest = sha256_hex(
        compact(
            {
                "trainRowIds": train_ids,
                "evalRowIds": eval_ids,
                "featureNames": feature_names,
            }
        )
    )
    if codes:
        selected = None  # any reason code makes selectedTrialId null
    return _select_response(
        run_id, selected, train_ids, eval_ids, feature_names, digest, codes
    )


def _parse_row(r):
    """Return a normalised row dict, or None when the row is malformed."""
    if not isinstance(r, dict):
        return None
    rid = r.get("id")
    if not isinstance(rid, str) or not rid:
        return None
    entity = r.get("entity")
    if not isinstance(entity, str) or not entity:
        return None
    event_time = parse_instant(r.get("eventTime"))
    prediction_time = parse_instant(r.get("predictionTime"))
    if event_time is None or prediction_time is None:
        return None
    version = r.get("version")
    if not is_safe_int(version, non_negative=True):
        return None
    split = r.get("split")
    if split not in ("TRAIN", "EVAL"):
        return None
    features_raw = r.get("features")
    if features_raw is None:
        features_raw = {}
    if not isinstance(features_raw, dict):
        return None
    features = {}
    for name, f in features_raw.items():
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(f, dict):
            return None
        avail = parse_instant(f.get("availableAt"))
        if avail is None:
            return None
        features[name] = avail  # feature "value" is opaque data; never parsed
    return {
        "id": rid,
        "entity": entity,
        "eventTime": event_time,
        "predictionTime": prediction_time,
        "version": version,
        "split": split,
        "features": features,
    }


def _parse_trial(t):
    if not isinstance(t, dict):
        return None
    tid = t.get("trialId")
    if not is_safe_int(tid, non_negative=True):
        return None
    status = t.get("status")
    if status not in ("SUCCEEDED", "FAILED"):
        return None
    # A non-finite / absent evalMetric is not malformed input: the spec only
    # says such a trial is not *eligible* ("only finite SUCCEEDED trials").
    return {"trialId": tid, "status": status, "evalMetric": t.get("evalMetric")}


def _build_dataset(parsed_rows, forbidden):
    # Deduplicate by [entity, UTC(eventTime)]: highest integer version wins,
    # then the UTF-8-byte-smallest ID.
    best = {}
    for r in parsed_rows:
        key = (r["entity"], r["eventTime"])
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        if r["version"] > cur["version"]:
            best[key] = r
        elif r["version"] == cur["version"] and utf8_key(r["id"]) < utf8_key(cur["id"]):
            best[key] = r
    retained = list(best.values())

    # A feature is eligible only if it appears in EVERY retained row, is not
    # forbidden, and its availableAt <= that row's predictionTime everywhere.
    names = None
    for r in retained:
        s = set(r["features"].keys())
        names = s if names is None else (names & s)
    names = names or set()
    eligible = []
    for name in names:
        if name in forbidden:
            continue
        if all(r["features"][name] <= r["predictionTime"] for r in retained):
            eligible.append(name)

    train_ids = sorted(
        (r["id"] for r in retained if r["split"] == "TRAIN"), key=utf8_key
    )
    eval_ids = sorted(
        (r["id"] for r in retained if r["split"] == "EVAL"), key=utf8_key
    )
    return train_ids, eval_ids, sorted(eligible, key=utf8_key)


# --------------------------------------------------------------------------
# phase: evaluate
# --------------------------------------------------------------------------
def _evaluate(body):
    run_id = body.get("runId")
    trial_id = body.get("selectedTrialId")
    digest = body.get("datasetDigest")
    codes = []

    # ---- lineage: runId + non-null integer trial + 64-lowercase-hex digest
    # must exactly match a stored successful selection. ----
    lineage_ok = (
        _valid_run_id(run_id)
        and is_safe_int(trial_id, non_negative=True)
        and isinstance(digest, str)
        and HEX64.match(digest) is not None
    )
    if lineage_ok:
        stored = _store.get(run_id)
        resp = stored["response"] if stored else None
        lineage_ok = bool(
            resp
            and not resp["reasonCodes"]
            and resp["selectedTrialId"] == trial_id
            and resp["datasetDigest"] == digest
        )
    if not lineage_ok:
        codes.append("INVALID_LINEAGE")

    invalid_input = False

    metric_floor = body.get("metricFloor")
    floor_ok = in_unit(metric_floor)
    if not floor_ok:
        invalid_input = True

    slices_raw = body.get("requiredSlices")
    if slices_raw is None:
        slices_raw = {}
    slices_ok = isinstance(slices_raw, dict) and all(
        isinstance(k, str) and k and in_unit(v) for k, v in slices_raw.items()
    )
    if not slices_ok:
        invalid_input = True

    bytes_processed = body.get("bytesProcessed")
    max_bytes = body.get("maxBytes")
    bytes_ok = is_safe_int(bytes_processed, non_negative=True) and is_safe_int(
        max_bytes, non_negative=True
    )
    if not bytes_ok:
        invalid_input = True

    rows_raw = body.get("rows")
    if rows_raw is None:
        rows_raw = []
    rows_is_list = isinstance(rows_raw, list)
    # An empty test set is not a usable evaluation input: with no rows there is
    # nothing to score against the floors, so the request is rejected outright
    # rather than reported as a pass on a null metric.
    if not rows_is_list or not rows_raw:
        invalid_input = True

    bad_row = False
    parsed = []
    if rows_is_list:
        for r in rows_raw:
            pr = _parse_test_row(r)
            if pr is None:
                bad_row = True
                break
            parsed.append(pr)

    if invalid_input:
        codes.append("INVALID_INPUT")
    if bad_row:
        codes.append("INVALID_TEST_ROW")

    # Byte gate still applies even when rows are empty/invalid.
    if bytes_ok and bytes_processed > max_bytes:
        codes.append("BYTE_LIMIT")

    missing_slice = False
    slice_floor_fail = False
    test_metric = None

    # Empty rows or any invalid row: testMetric is null and the aggregate and
    # required-slice checks are skipped entirely.
    if rows_is_list and not bad_row and parsed:
        test_metric = round12(
            sum(1 for r in parsed if r["label"] == r["prediction"]) / len(parsed)
        )
        if floor_ok and test_metric < metric_floor:
            codes.append("AGGREGATE_FLOOR")
        if slices_ok:
            for name in slices_raw:
                sub = [r for r in parsed if r["slice"] == name]
                if not sub:
                    missing_slice = True
                    codes.append("MISSING_SLICE:" + name)
                    continue
                acc = round12(
                    sum(1 for r in sub if r["label"] == r["prediction"]) / len(sub)
                )
                if acc < slices_raw[name]:
                    slice_floor_fail = True
                    codes.append("SLICE_FLOOR:" + name)

    # criticalSlicePass does NOT summarise the aggregate or byte gates.
    critical = not (
        invalid_input
        or not lineage_ok
        or bad_row
        or missing_slice
        or slice_floor_fail
    )
    codes = sorted_unique_codes(codes)
    return 200, {
        "runId": run_id if isinstance(run_id, str) else None,
        "selectedTrialId": trial_id if is_safe_int(trial_id, non_negative=True) else None,
        "datasetDigest": digest if isinstance(digest, str) else None,
        "testMetric": test_metric,
        "criticalSlicePass": critical,
        "decision": "admit" if (not codes and test_metric is not None) else "reject",
        "bytesProcessed": bytes_processed if is_safe_int(
            bytes_processed, non_negative=True
        ) else None,
        "reasonCodes": codes,
    }


def _parse_test_row(r):
    if not isinstance(r, dict):
        return None
    label = r.get("label")
    pred = r.get("prediction")
    if not is_safe_int(label, non_negative=True) or label not in (0, 1):
        return None
    if not is_safe_int(pred, non_negative=True) or pred not in (0, 1):
        return None
    sl = r.get("slice")
    if not isinstance(sl, str) or not sl:
        return None
    return {"label": label, "prediction": pred, "slice": sl}
