"""Q10 needs a real Hugging Face repository, so this makes one.

The grader will not take any other host -- it answers "URL must be a Hugging Face
repository URL" -- and the card has to carry that student's own carbon numbers,
which differ per email. So there is no URL to hand out and nothing to pre-build:
a repo has to exist, per student, with their values in it.

This creates one on demand under whichever account owns HF_TOKEN, writes the card
the caller computed, and returns the URL. Without a token configured it refuses
politely, and the solver falls back to telling the student to publish it
themselves -- so the service still works, it just cannot do this one step.
"""
import hashlib
import os
import re

HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()
HF_OWNER = os.environ.get("HF_OWNER", "").strip()
REPO_PREFIX = os.environ.get("HF_REPO_PREFIX", "tds-ga8-carbon").strip()

# The card is short and fixed in shape. Anything bigger, or missing the block the
# grader reads, is not a carbon card and is not worth a repo.
MAX_CARD_BYTES = 8000
_REQUIRED = ("co2_eq_emissions:", "emissions:", "source: codecarbon")


def configured() -> bool:
    return bool(HF_TOKEN and HF_OWNER)


def repo_id_for(email: str) -> str:
    """Stable per student, and it does not put their address in a public name."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]
    return f"{HF_OWNER}/{REPO_PREFIX}-{digest}"


def _looks_like_card(card: str) -> bool:
    head = card.lstrip()
    if not head.startswith("---"):
        return False
    return all(token in card for token in _REQUIRED)


def publish(email: str, card: str):
    """Return (status, payload). Never raises."""
    if not configured():
        return 503, {
            "error": "HF_NOT_CONFIGURED",
            "detail": "This deployment has no Hugging Face account attached, so it "
                      "cannot publish the card for you. Create a public repo and "
                      "paste the card into its README.md yourself.",
        }
    if not isinstance(card, str) or not _looks_like_card(card):
        return 400, {"error": "INVALID_CARD"}
    if len(card.encode("utf-8")) > MAX_CARD_BYTES:
        return 400, {"error": "CARD_TOO_LARGE"}
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", (email or "").strip()):
        return 400, {"error": "INVALID_EMAIL"}

    repo_id = repo_id_for(email)
    try:
        from huggingface_hub import HfApi

        api = HfApi(token=HF_TOKEN)
        api.create_repo(repo_id, repo_type="model", private=False, exist_ok=True)
        api.upload_file(
            path_or_fileobj=card.encode("utf-8"),
            path_in_repo="README.md",
            repo_id=repo_id,
            repo_type="model",
            commit_message="carbon accounting card",
        )
    except Exception as exc:  # noqa: BLE001 - surface the reason, do not crash the service
        return 502, {"error": "HF_PUBLISH_FAILED", "detail": str(exc)[:300]}

    return 200, {"url": f"https://huggingface.co/{repo_id}", "repo_id": repo_id}
