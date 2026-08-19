"""TDS 2026-05 GA8 — one service, seven graded endpoints (Q1..Q7)."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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


async def _dispatch(path: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    try:
        status, payload = ROUTES[path](body)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
    return JSONResponse(status_code=status, content=payload)


for _p in list(ROUTES):
    def _make(p):
        async def _h(request: Request):
            return await _dispatch(p, request)
        return _h
    app.post(_p)(_make(_p))
