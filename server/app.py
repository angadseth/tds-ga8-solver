"""TDS 2026-05 GA8 — one service, seven graded endpoints (Q1..Q7).

Two ways to reach the same handlers:

    POST /bqml                      a deployment of your own
    POST /ga8/<your-email>/bqml     one deployment shared by many students

The second exists because Q2 and Q5 remember things between requests -- a
select's runId, a freeze's inputs -- and two students grading simultaneously
would otherwise collide on the same identifier. The email in the path namespaces
that state, so everybody gets their own. It changes nothing else: the handlers
never read it, and the answers are identical either way.
"""
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from _common import TENANT

import q1_corpus, q2_bqml, q3_promote, q4_adapt, q5_quantize, q6_pipeline, q7_bundle

app = FastAPI(title="TDS GA8 Service")

ROUTES = {
    "/build-corpus": q1_corpus.handle,
    "/bqml": q2_bqml.handle,
    "/promote": q3_promote.handle,
    "/adapt": q4_adapt.handle,
    "/quantize": q5_quantize.handle,
    "/pipeline": q6_pipeline.handle,
    "/verify-bundle": q7_bundle.handle,
}


@app.get("/")
async def root():
    return {"service": "tds-ga8", "endpoints": sorted(ROUTES)}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


MAX_TENANT_LEN = 120


async def _dispatch(path: str, request: Request, tenant: str = ""):
    TENANT.set(tenant)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    try:
        status, payload = ROUTES[path](body)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    return JSONResponse(status_code=status, content=payload)


def _tenant(raw: str) -> str:
    """Normalise the path segment into a stable per-student namespace."""
    return unquote(raw or "").strip().lower()[:MAX_TENANT_LEN]


for _p in list(ROUTES):
    def _make(p):
        async def _own(request: Request):
            return await _dispatch(p, request)

        async def _shared(email: str, request: Request):
            return await _dispatch(p, request, _tenant(email))

        return _own, _shared

    _own_h, _shared_h = _make(_p)
    app.post(_p)(_own_h)
    app.post("/ga8/{email}" + _p)(_shared_h)


@app.get("/ga8/{email}")
async def shared_root(email: str):
    """What a student sees if they open their own URL in a browser."""
    return {
        "service": "tds-ga8",
        "you": _tenant(email),
        "endpoints": sorted(ROUTES),
        "note": "Paste this URL into all seven of the Q1-Q7 answer boxes.",
    }
