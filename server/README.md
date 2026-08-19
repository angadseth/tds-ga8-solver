# The Q1–Q7 service

Seven of the ten GA8 questions grade a **URL**, not a value. The marker posts a fixed set of
requests to your endpoint and scores how it answers, so nobody can hand you a number — you have
to run a server.

This folder is that server: one FastAPI app, seven routes, scoring full marks on all seven
(10/10 on the real grader, checked twice).

| Route | Question |
|---|---|
| `/build-corpus` | Q1 · Immutable training corpus |
| `/bqml` | Q2 · Leakage-safe BQML gate |
| `/promote` | Q3 · MLflow evidence promotion |
| `/adapt` | Q4 · PEFT repair |
| `/quantize` | Q5 · Quantized model admission |
| `/pipeline` | Q6 · Content-addressed pipeline |
| `/verify-bundle` | Q7 · Verifiable model bundle |

## Run it locally first

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
curl localhost:8000/          # lists the seven routes
```

## Deploy it — one click

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/angadseth/tds-ga8-solver)

Sign in with GitHub, press Apply, wait about two minutes. Render prints a URL like
`https://tds-ga8-xxxx.onrender.com`. **That URL is your answer to all seven questions** — paste
it into each of the seven boxes, or let the solver panel fill them for you.

> **Free instances sleep after 15 minutes idle.** A sleeping service takes ~50 s to wake, which
> is long enough for the first graded request to time out. Open `https://your-url/healthz` in a
> tab and wait for `{"ok":true}` **immediately before** you press Save on the exam. Do that and
> the free plan is fine.

### Or Cloud Run, if you have a Google account

No Dockerfile needed — it builds from source:

```bash
gcloud run deploy tds-ga8 --source . \n  --region asia-south1 --allow-unauthenticated \n  --min-instances=1 --max-instances=1
```

Never sleeps, so there is nothing to wake. `--min-instances=1` keeps it warm.

### Or anywhere else

There is a `Dockerfile`, so Fly.io, Railway, Koyeb and plain Docker all work unchanged.
`ngrok http 8000` in front of a local `uvicorn` works too, as long as the tunnel stays up for
the whole grading run.

## Why you cannot just borrow somebody else's URL

Three of these questions keep state in memory: Q2 stores each `runId` from the *select* call so
the *evaluate* call that follows can check the lineage against it, and Q5 stores each freeze so a
replay with different input can return 409. Both are `dict`s living inside one process.

Point a hundred students at one shared service and it scales to several instances. The grader's
`select` lands on one, its `evaluate` on another, the lookup misses, and the answer comes back
`INVALID_LINEAGE`. Marks vanish unpredictably — some runs full, some not, with nothing in the
message to explain it. Cap it at one instance instead and everyone queues behind a single
process until requests time out.

**`--max-instances=1` is what makes this correct, and it only works when the service is yours.**

## Things that cost marks, learned the hard way

- **Redeploy after every edit, then re-grade.** A source change that is not deployed shows up as
  a mysteriously unchanged score. One question sat at 1.28/1.5 for a day because an old revision
  was still serving.
- **Grade twice before believing a score.** The marker's score moves slightly on unchanged code.
  One reading is not evidence.
- **Never print non-ASCII to stdout.** The grader sends payloads containing non-ASCII text. On a
  console that is not UTF-8, an unguarded `print` raises `UnicodeEncodeError`, the app turns that
  into a blanket HTTP 400, and the question scores **zero**. If you add logging, keep it behind an
  env var, `ensure_ascii=True`, wrapped in its own try/except, inside the handler's try block.
- **Q5 must return 409** when a freeze is replayed with different input. Returning 200 there made
  the grader reject the whole endpoint — not one check, the entire question.
- **An empty test-row list is invalid input** (Q2), not a pass on a null metric.
- **A rejected version still reports only its own rejection code** (Q3) — do not also run the
  evidence gates over it.
