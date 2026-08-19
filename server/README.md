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

## Deploy it

The service is a plain FastAPI app, so anywhere that runs Python will do. Cloud Run needs no
Dockerfile — it builds from the source directly:

```bash
gcloud run deploy tds-ga8 --source . \
  --region asia-south1 --allow-unauthenticated --min-instances=1
```

The command prints a URL. **That URL is your answer to all seven questions** — paste it into
each of the seven boxes, or let the solver's panel fill them for you.

`--min-instances=1` keeps one instance warm. Without it the first request of each grading run
pays cold-start and can time out.

If you would rather not use Cloud Run: Render, Fly.io and Railway all deploy this unchanged, and
`ngrok http 8000` in front of a local `uvicorn` works too, as long as the tunnel stays up for the
whole grading run.

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
