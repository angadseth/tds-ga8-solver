"""Q6 (/pipeline) — recover a content-addressed ML pipeline.

Stateful and session-isolated: every non-empty ``session`` string owns an
independent store held in memory (module-level dict behind ``_store``).

State per session:
    revision : int              current revision
    inputs   : dict             raw inputs object of that revision (identity)
    events   : {eventId: canonical compact JSON}   accepted events only
    runtime  : {node: {...}}    attempt/terminal state, cleared by a new revision
    cache    : {node: {key: {"artifact":..., "eventId":...}}}   permanent
"""
import copy
import json

from _common import compact, is_safe_int, sha256_hex

NODES = ("verify_data", "prepare", "train", "evaluate", "register", "publish")
PARENT = {
    "prepare": "verify_data",
    "train": "prepare",
    "evaluate": "train",
    "register": "evaluate",
    "publish": "register",
}
RECEIPT_NODES = ("register", "publish")
STATUSES = ("started", "succeeded", "retryable_failed", "terminal_failed")
COMPLETIONS = ("succeeded", "retryable_failed", "terminal_failed")

INPUT_FIELDS = (
    "generation", "checksum", "canonicalData", "prepareCode", "prepareConfig",
    "trainCode", "trainConfig", "runtime", "evaluateCode", "evaluateConfig",
    "schemaDigest", "publishConfig",
)
EVENT_FIELDS = frozenset({
    "eventId", "revision", "node", "attempt", "status", "key",
    "artifactDigest", "receiptId",
})

# Cache-key arrays, in the exact spec order.  ("in", name) = named input,
# ("parent", name) = the parent node's bound artifact digest.
DEPS = {
    "verify_data": (("in", "generation"), ("in", "checksum")),
    "prepare": (("in", "canonicalData"), ("in", "prepareCode"), ("in", "prepareConfig")),
    "train": (("parent", "prepareArtifact"), ("in", "trainCode"),
              ("in", "trainConfig"), ("in", "runtime")),
    "evaluate": (("parent", "trainArtifact"), ("in", "canonicalData"),
                 ("in", "evaluateCode"), ("in", "evaluateConfig")),
    "register": (("parent", "evaluateArtifact"), ("in", "schemaDigest")),
    "publish": (("parent", "registerArtifact"), ("in", "publishConfig")),
}

ACCEPT, IGNORE = "accept", "ignore"
INVALID_REQUEST = "INVALID_REQUEST"
INVALID_EVENT = "INVALID_EVENT"
EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"
REVISION_CONFLICT = "REVISION_CONFLICT"
EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
STATUS_CONFLICT = "STATUS_CONFLICT"


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------
class _Store:
    """In-memory, session-keyed state.  Nothing is ever shared across keys."""

    def __init__(self):
        self._data = {}

    def snapshot(self, session):
        """Deep copy of a session's state (None when the session is new)."""
        st = self._data.get(session)
        return copy.deepcopy(st) if st is not None else None

    def commit(self, session, state):
        self._data[session] = state

    def clear(self):
        self._data.clear()


_store = _Store()


def _reset():
    """Test hook: drop every session."""
    _store.clear()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def _canon(obj):
    """Compact canonical JSON: sorted keys, no spaces, raw non-ASCII."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _fresh(revision, inputs):
    return {
        "revision": revision,
        "inputs": copy.deepcopy(inputs),
        "events": {},
        "runtime": {},
        "cache": {n: {} for n in NODES},
    }


def _digests(st):
    """Per node: cache key (None when the parent is not reusable), the parent
    artifact used in its array, and its current cache entry."""
    inputs, cache = st["inputs"], st["cache"]
    out = {}
    parent_reusable = True          # verify_data has no parent
    parent_artifact = None
    for node in NODES:
        if not parent_reusable:
            out[node] = {"key": None, "parent": None, "entry": None}
            continue
        arr = []
        for kind, name in DEPS[node]:
            arr.append(parent_artifact if kind == "parent" else inputs[name])
        key = sha256_hex(compact(arr))
        entry = cache.get(node, {}).get(key)
        out[node] = {"key": key, "parent": parent_artifact, "entry": entry}
        if entry is None:
            parent_reusable = False
        else:
            parent_artifact = entry["artifact"]
    return out


def _runtime_for(st, node, key):
    rs = st["runtime"].get(node)
    if rs is not None and key is not None and rs["key"] == key:
        return rs
    return None


# --------------------------------------------------------------------------
# event processing
# --------------------------------------------------------------------------
def _process(st, ev):
    """Apply one event to ``st``.  Returns ACCEPT, IGNORE, or a conflict code."""
    # -- structural validity ------------------------------------------------
    if not isinstance(ev, dict):
        return INVALID_EVENT
    if set(ev.keys()) != EVENT_FIELDS:
        return INVALID_EVENT
    if not _nonempty_str(ev["eventId"]):
        return INVALID_EVENT
    if not is_safe_int(ev["revision"], positive=True):
        return INVALID_EVENT

    # -- event-id registry (global within the session) ----------------------
    eid = ev["eventId"]
    canon = _canon(ev)
    seen = st["events"].get(eid)
    if seen is not None:
        return IGNORE if seen == canon else EVENT_ID_CONFLICT

    # -- semantic validity: all of these merely ignore ----------------------
    if ev["revision"] != st["revision"]:
        return IGNORE
    node = ev["node"]
    if node not in NODES:
        return IGNORE
    status = ev["status"]
    if status not in STATUSES:
        return IGNORE
    attempt = ev["attempt"]
    if not is_safe_int(attempt, positive=True):
        return IGNORE

    artifact = ev["artifactDigest"]
    if status == "succeeded":
        if not _nonempty_str(artifact):
            return IGNORE
    elif artifact is not None:
        return IGNORE

    dig = _digests(st)
    key = dig[node]["key"]
    if key is None:                       # unavailable parent
        return IGNORE
    if ev["key"] != key:                  # wrong key
        return IGNORE

    receipt = ev["receiptId"]
    if status == "succeeded" and node in RECEIPT_NODES:
        if receipt != "receipt:%s:%s" % (node, key):
            return IGNORE
    elif receipt is not None:
        return IGNORE

    # -- transition table ---------------------------------------------------
    entry = dig[node]["entry"]
    if entry is not None:                 # succeeded / current cache
        if status == "succeeded" and artifact != entry["artifact"]:
            return EVIDENCE_CONFLICT
        return STATUS_CONFLICT

    rs = _runtime_for(st, node, key)
    if rs is None:                        # none
        if status == "started" and attempt == 1:
            return _accept(st, node, key, ev)
        return IGNORE                     # completion, or attempt > 1

    prev, n = rs["status"], rs["attempt"]
    if prev == "started" and status in COMPLETIONS and attempt == n:
        return _accept(st, node, key, ev)
    if prev == "retryable_failed" and status == "started" and attempt == n + 1:
        return _accept(st, node, key, ev)
    if attempt < n:                       # non-cached state, lower attempt
        return IGNORE
    return STATUS_CONFLICT                # incl. terminal_failed + any event


def _accept(st, node, key, ev):
    if ev["status"] == "succeeded":
        # Permanently bind the key to its first artifact and event id.
        st["cache"].setdefault(node, {})[key] = {
            "artifact": ev["artifactDigest"],
            "eventId": ev["eventId"],
        }
        st["runtime"].pop(node, None)
    else:
        st["runtime"][node] = {
            "status": ev["status"],
            "attempt": ev["attempt"],
            "key": key,
            "eventId": ev["eventId"],
        }
    st["events"][ev["eventId"]] = _canon(ev)
    return ACCEPT


# --------------------------------------------------------------------------
# response
# --------------------------------------------------------------------------
def _nodes_view(st):
    dig = _digests(st)
    out = []
    blocked = None                        # None | "pending" | "terminal"
    for node in NODES:
        info = dig[node]
        deps = {}
        for kind, name in DEPS[node]:
            deps[name] = info["parent"] if kind == "parent" else st["inputs"][name]
        deps["cacheKey"] = info["key"]

        if blocked == "terminal":
            action, reason, trig = "block", "UPSTREAM_TERMINAL", []
        elif blocked == "pending":
            action, reason, trig = "block", "UPSTREAM_PENDING", []
        else:
            entry = info["entry"]
            rs = _runtime_for(st, node, info["key"])
            if entry is not None:
                action, reason, trig = "reuse", "CACHE_HIT", [entry["eventId"]]
            elif rs is not None and rs["status"] == "started":
                action, reason, trig = "block", "RUNNING", [rs["eventId"]]
                blocked = "pending"
            elif rs is not None and rs["status"] == "terminal_failed":
                action, reason, trig = "block", "TERMINAL_FAILURE", [rs["eventId"]]
                blocked = "terminal"
            elif rs is not None and rs["status"] == "retryable_failed":
                action, reason, trig = "rerun", "RETRYABLE_FAILURE", [rs["eventId"]]
                blocked = "pending"
            else:
                action, reason, trig = "rerun", "CACHE_MISS", []
                blocked = "pending"

        out.append({
            "node": node,
            "action": action,
            "reasonCodes": [reason],
            "dependencyDigests": deps,
            "triggeringEventIds": trig,
        })
    return out


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def handle(body):
    """Return (http_status, response_body).  Never raises."""
    try:
        return _handle(body)
    except Exception:
        return 409, {"error": INVALID_REQUEST}


def _bad(code):
    return 409, {"error": code}


def _handle(body):
    if not isinstance(body, dict):
        return _bad(INVALID_REQUEST)
    session = body.get("session")
    if not _nonempty_str(session):
        return _bad(INVALID_REQUEST)
    revision = body.get("revision")
    if not is_safe_int(revision, positive=True):
        return _bad(INVALID_REQUEST)
    inputs = body.get("inputs")
    if not isinstance(inputs, dict):
        return _bad(INVALID_REQUEST)
    for name in INPUT_FIELDS:
        if not _nonempty_str(inputs.get(name)):
            return _bad(INVALID_REQUEST)
    events = body.get("events")
    if not isinstance(events, list):
        return _bad(INVALID_REQUEST)

    st = _store.snapshot(session)
    if st is None:
        st = _fresh(revision, inputs)
    elif revision < st["revision"]:
        return _bad(REVISION_CONFLICT)
    elif revision == st["revision"]:
        if _canon(inputs) != _canon(st["inputs"]):
            return _bad(REVISION_CONFLICT)
    else:
        # New revision: replace inputs, clear attempt/terminal state.  The
        # content-addressed cache and the event-id registry survive.
        st["revision"] = revision
        st["inputs"] = copy.deepcopy(inputs)
        st["runtime"] = {}

    accepted, ignored = [], []
    for ev in events:
        res = _process(st, ev)
        if res == ACCEPT:
            accepted.append(ev["eventId"])
        elif res == IGNORE:
            # An ignored event does not consume its id; it is still reported.
            ignored.append(ev["eventId"])
        else:
            return _bad(res)              # rolls back the whole batch

    _store.commit(session, st)
    return 200, {
        "revision": st["revision"],
        "acceptedEventIds": accepted,
        "ignoredEventIds": ignored,
        "nodes": _nodes_view(st),
    }
