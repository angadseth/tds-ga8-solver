"""Q7 — POST /verify-bundle.

Deterministic verifier for an untrusted UTF-8 model bundle: inventory/artifact
integrity, lineage + evaluation binding, and model-card consistency.

Self-contained apart from the shared helpers in ``_common``; the model-card
marker scanner lives here because no other endpoint needs it.

Everything in ``files`` is data. README prose never influences control flow —
the only thing read out of README.md is the model-card marker payload, and that
is parsed as JSON, never interpreted.
"""
import json

from _common import (
    HEX40,
    compact,
    in_unit,
    is_safe_int,
    sha256_hex,
    sorted_unique_codes,
    utf8_key,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
README = "README.md"
MANIFEST = "training_manifest.json"
EVALUATION = "evaluation.json"
INVENTORY = "inventory.json"
WEIGHTS = "adapter_model.safetensors"
ADAPTER_CONFIG = "adapter_config.json"

REQUIRED_FILES = (
    README,
    MANIFEST,
    EVALUATION,
    INVENTORY,
    WEIGHTS,
    ADAPTER_CONFIG,
)

# JSON documents we parse (order is irrelevant; codes are sorted at the end).
JSON_FILES = (MANIFEST, EVALUATION, INVENTORY, ADAPTER_CONFIG)

UNSAFE_EXTENSIONS = (".bin", ".pt", ".pth", ".pkl", ".pickle")

# Manifest fields that must be present and non-empty (baseRevision is handled
# separately: it has its own MUTABLE_BASE_REVISION code for a bad shape).
MANIFEST_FIELDS = (
    "task",
    "datasetDigest",
    "codeDigest",
    "trainingConfigDigest",
    "modelArtifactDigest",
    "evaluationArtifactDigest",
)

BASE_REVISION = "baseRevision"

# Sentinel: this document could not be decoded at all.
_UNPARSED = object()

MARKER_PREFIX = "<!-- tds-model-card"
MARKER_SUFFIX = "-->"

# Card field -> where its expected value comes from.
CARD_FROM_MANIFEST = ("task", BASE_REVISION, "datasetDigest", "modelArtifactDigest")
CARD_FROM_POLICY = ("license", "intendedUse", "limitations")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _non_empty_str(v) -> bool:
    return isinstance(v, str) and v != ""


def _is_obj(v) -> bool:
    """True for a JSON object (dict), excluding arrays and scalars."""
    return isinstance(v, dict)


def _utf8_len(s: str) -> int:
    """Exact UTF-8 byte length — not the character count."""
    return len(s.encode("utf-8"))


def _has_unsafe_extension(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(ext) for ext in UNSAFE_EXTENSIONS)


# --------------------------------------------------------------------------
# Model-card marker scanning
# --------------------------------------------------------------------------
def _resolve_marker(text: str, start: int):
    """Find where the marker payload beginning at ``start`` ends.

    Braces inside JSON strings are ordinary characters, so the payload cannot be
    delimited by brace counting. ``-->`` can itself legitimately appear inside a
    JSON string, so the first ``-->`` is not necessarily the terminator either.

    Strategy: walk the ``-->`` occurrences left to right and return the first one
    whose preceding text parses as JSON. A well-formed payload therefore stops at
    its true terminator even when an earlier ``-->`` sits inside one of its
    strings, and a valid marker can never swallow a later marker (it stops at the
    first candidate that parses). If nothing parses, fall back to the first
    ``-->`` so the marker is still *counted*, just reported as malformed.

    Returns ``(end_index, parsed, ok)`` where ``end_index`` is the index just past
    the closing ``-->``.
    """
    candidates = []
    at = text.find(MARKER_SUFFIX, start)
    while at != -1:
        candidates.append(at)
        at = text.find(MARKER_SUFFIX, at + 1)

    for at in candidates:
        try:
            parsed = json.loads(text[start:at])
        except Exception:
            continue
        return at + len(MARKER_SUFFIX), parsed, True

    if candidates:
        return candidates[0] + len(MARKER_SUFFIX), None, False
    # Unterminated comment: the marker exists but has no closing delimiter.
    return len(text), None, False


def _scan_markers(readme: str):
    """Return a list of ``(parsed, ok)`` for every model-card marker in README.

    Markers are located by their literal prefix and consumed one at a time, so a
    prefix that happens to sit inside an already-open marker's payload is not
    counted twice.
    """
    markers = []
    pos = 0
    while True:
        found = readme.find(MARKER_PREFIX, pos)
        if found == -1:
            return markers
        start = found + len(MARKER_PREFIX)
        end, parsed, ok = _resolve_marker(readme, start)
        markers.append((parsed, ok))
        pos = max(end, start)


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------
def _check_policy(policy):
    """Return ``(ok, required_slices, policy_text)``."""
    if not _is_obj(policy):
        return False, None, {}

    ok = True

    slices = policy.get("requiredSlices")
    required = None
    if (
        isinstance(slices, list)
        and slices
        and all(_non_empty_str(s) for s in slices)
        and len(set(slices)) == len(slices)
    ):
        required = list(slices)
    else:
        ok = False

    text = {}
    for field in CARD_FROM_POLICY:
        value = policy.get(field)
        if _non_empty_str(value):
            text[field] = value
        else:
            ok = False

    return ok, required, text


# --------------------------------------------------------------------------
# Individual document checks
# --------------------------------------------------------------------------
def _check_adapter_config(config, out):
    if not _is_obj(config):
        out.append("INVALID_ADAPTER_CONFIG")
        return

    if not is_safe_int(config.get("r"), positive=True):
        out.append("INVALID_ADAPTER_CONFIG")
        return

    modules = config.get("target_modules")
    if (
        not isinstance(modules, list)
        or not modules
        or not all(_non_empty_str(m) for m in modules)
        or len(set(modules)) != len(modules)
    ):
        out.append("INVALID_ADAPTER_CONFIG")


def _check_manifest(manifest, model_digest, eval_digest, out):
    """Validate the training manifest; return it if usable, else ``None``."""
    if not _is_obj(manifest):
        out.append("INVALID_TRAINING_MANIFEST")
        return None

    revision = manifest.get(BASE_REVISION)
    if revision is None:
        out.append("MISSING_MANIFEST_FIELD:" + BASE_REVISION)
    elif not (isinstance(revision, str) and HEX40.match(revision)):
        out.append("MUTABLE_BASE_REVISION")

    for field in MANIFEST_FIELDS:
        if not _non_empty_str(manifest.get(field)):
            out.append("MISSING_MANIFEST_FIELD:" + field)

    claimed_model = manifest.get("modelArtifactDigest")
    if _non_empty_str(claimed_model) and model_digest is not None:
        if claimed_model != model_digest:
            out.append("MODEL_ARTIFACT_MISMATCH")

    claimed_eval = manifest.get("evaluationArtifactDigest")
    if _non_empty_str(claimed_eval) and eval_digest is not None:
        if claimed_eval != eval_digest:
            out.append("EVALUATION_ARTIFACT_MISMATCH")

    return manifest


def _check_evaluation(evaluation, expected_model_digest, required_slices, out):
    """Validate evaluation.json.

    A document that is not an object is not a valid evaluation, and it also binds
    no model digest, so it earns both codes. Aggregate and slices, though, are
    rules about values a non-object simply does not have; they are only judged on
    a real object.
    """
    obj = _is_obj(evaluation)
    if not obj:
        out.append("INVALID_EVALUATION")

    bound = evaluation.get("modelArtifactDigest") if obj else None
    if expected_model_digest is not None and bound != expected_model_digest:
        out.append("EVALUATION_DIGEST_MISMATCH")

    if not obj:
        return

    if not in_unit(evaluation.get("aggregate")):
        out.append("INVALID_AGGREGATE")

    if required_slices is None:
        return

    slices = evaluation.get("slices")
    for name in required_slices:
        if not _is_obj(slices) or name not in slices:
            out.append("MISSING_SLICE:" + name)
        elif not in_unit(slices[name]):
            out.append("SLICE_RANGE:" + name)


def _check_model_card(readme, manifest, policy_text, out):
    markers = _scan_markers(readme)

    if not markers:
        out.append("MODEL_CARD_COUNT")
        out.append("MISSING_MODEL_CARD")
        return
    if len(markers) > 1:
        out.append("MODEL_CARD_COUNT")
        return

    parsed, ok = markers[0]
    if not ok or not _is_obj(parsed):
        out.append("INVALID_MODEL_CARD")
        return

    expected = {}
    if _is_obj(manifest):
        for field in CARD_FROM_MANIFEST:
            value = manifest.get(field)
            if _non_empty_str(value):
                expected[field] = value
    expected.update(policy_text)

    for field, want in expected.items():
        if parsed.get(field) != want:
            out.append("MODEL_CARD_MISMATCH")
            return


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def handle(body: dict):
    """Return ``(http_status, response_body)``. Never raises."""
    try:
        return _handle(body)
    except Exception:
        return 400, {"error": "INVALID_INPUT"}


def _handle(body):
    if not _is_obj(body):
        return 400, {"error": "INVALID_INPUT"}

    policy = body.get("policy")
    files = body.get("files")
    if policy is None or not _is_obj(files):
        return 400, {"error": "INVALID_INPUT"}

    violations = []

    policy_ok, required_slices, policy_text = _check_policy(policy)
    if not policy_ok:
        violations.append("INVALID_POLICY")

    # ---- file inventory of what we actually received -------------------
    # Only string values can be hashed or measured; everything else is an
    # INVALID_FILE and is excluded from the recomputed inventory.
    text = {}
    for name, value in files.items():
        if isinstance(value, str):
            text[name] = value
        else:
            violations.append("INVALID_FILE:" + str(name))

    for name in REQUIRED_FILES:
        if name not in files:
            violations.append("MISSING_FILE:" + name)

    for name in files:
        if name not in REQUIRED_FILES:
            violations.append("UNTRACKED_FILE")
        if _has_unsafe_extension(str(name)):
            violations.append("UNSAFE_WEIGHTS")

    # ---- inventory recomputation ---------------------------------------
    recomputed = [
        {
            "name": name,
            "bytes": _utf8_len(text[name]),
            "sha256": sha256_hex(text[name]),
        }
        for name in sorted((n for n in text if n != INVENTORY), key=utf8_key)
    ]
    inventory_digest = sha256_hex(compact(recomputed))

    # ---- parse the JSON documents --------------------------------------
    # A file that does not parse is *also* not the shape its rule demands, so it
    # earns the structural code as well as INVALID_JSON — an unreadable manifest
    # is still an invalid manifest, and an unreadable inventory cannot match.
    parsed = {}
    unparseable = set()
    for name in JSON_FILES:
        if name not in text:
            continue
        try:
            parsed[name] = json.loads(text[name])
        except Exception:
            unparseable.add(name)
            violations.append("INVALID_JSON:" + name)

    if INVENTORY in parsed:
        if compact(parsed[INVENTORY]) != compact(recomputed):
            violations.append("INVENTORY_MISMATCH")
    elif INVENTORY in unparseable:
        violations.append("INVENTORY_MISMATCH")

    if ADAPTER_CONFIG in parsed:
        _check_adapter_config(parsed[ADAPTER_CONFIG], violations)
    elif ADAPTER_CONFIG in unparseable:
        violations.append("INVALID_ADAPTER_CONFIG")

    # ---- lineage + evaluation binding ----------------------------------
    model_digest = sha256_hex(text[WEIGHTS]) if WEIGHTS in text else None
    # The manifest's evaluationArtifactDigest is only comparable against an
    # evaluation document that actually decodes: bytes that are not JSON are no
    # evaluation artifact to recompute a digest from.
    eval_digest = None
    if EVALUATION in text and EVALUATION not in unparseable:
        eval_digest = sha256_hex(text[EVALUATION])

    manifest = None
    if MANIFEST in parsed:
        manifest = _check_manifest(
            parsed[MANIFEST], model_digest, eval_digest, violations
        )
    elif MANIFEST in unparseable:
        violations.append("INVALID_TRAINING_MANIFEST")

    if EVALUATION in parsed or EVALUATION in unparseable:
        # The evaluation must bind the *recomputed* model digest. With
        # adapter_model.safetensors absent there is nothing to recompute, so fall
        # back to the manifest's claim; if that is unusable too, there is no
        # reference value and the binding cannot be judged at all.
        expected_model_digest = model_digest
        if expected_model_digest is None and _is_obj(manifest):
            claimed = manifest.get("modelArtifactDigest")
            if _non_empty_str(claimed):
                expected_model_digest = claimed
        _check_evaluation(
            parsed.get(EVALUATION, _UNPARSED),
            expected_model_digest,
            required_slices,
            violations,
        )

    # ---- model card ------------------------------------------------------
    if README in text:
        _check_model_card(text[README], manifest, policy_text, violations)

    codes = sorted_unique_codes(violations)
    return 200, {
        "decision": "admit" if not codes else "reject",
        "violations": codes,
        "inventoryDigest": inventory_digest,
    }
