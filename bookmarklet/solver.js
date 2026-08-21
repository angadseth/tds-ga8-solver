/**
 * Runs on the GA8 exam page itself.
 *
 * Same-origin, so it can read the email you are signed in as, derive your Q8/Q9/Q10
 * answers in this tab, and type them into the boxes. Nothing is sent anywhere.
 *
 * Q1-Q7 are graded by calling a server, so there is no value to fill: the hosted
 * service answers for you at a path of your own, and goes into all seven boxes.
 *
 * Saving is a button rather than something that happens on load -- it overwrites
 * whatever you submitted before, so it should be your decision, made after you
 * have looked at what got filled in.
 */
import { solveEmail, serviceUrlFor, QUESTION_IDS, SERVER_IDS } from "../assets/engine.js";

const PANEL_ID = "ga8-solver-panel";

/** One entry per student. The old shared key handed the first student who used a
 *  browser their repo back on the next student's run, and the emissions in it are
 *  not that second student's number -- a wrong answer that looks filled in. */
const repoKey = (email) => "ga8-solver-hf-repo:" + email.trim().toLowerCase();

/** Written by an earlier version of this panel that let you paste your own
 *  deployment. That deployment answers Q1-Q7 perfectly well, but it has no
 *  Hugging Face account attached, so Q10's publish was being sent there and
 *  refused. Nothing writes these any more, and nothing should read them. */
const DEAD_KEYS = ["ga8-solver-service-url", "ga8-solver-hf-repo"];

const HF_RE = /^https:\/\/huggingface\.co\/[^/]+\/[^/]+/;

const WEIGHTS = [
  ["q-immutable-training-corpus-server", "Q1", 1.5, false],
  ["q-leakage-safe-bqml-server", "Q2", 1.5, false],
  ["q-mlflow-evidence-promotion-server", "Q3", 1.25, false],
  ["q-peft-repair-server", "Q4", 2, false],
  ["q-quantized-model-admission-server", "Q5", 1.25, false],
  ["q-content-addressed-pipeline-server", "Q6", 1.5, false],
  ["q-verifiable-model-bundle-server", "Q7", 1, false],
  ["q-lora-quant-budget-server", "Q8", 2, true],
  ["q-mlflow-fingerprint-server", "Q9", 2.5, true],
  ["q-modelcard-carbon-server", "Q10", 2.5, true],
];

const TOTAL = WEIGHTS.reduce((a, w) => a + w[2], 0);
const Q10_ROW = WEIGHTS[WEIGHTS.length - 1];

async function main() {
  const panel = mountPanel();
  const say = (msg, kind = "") => {
    panel.status.textContent = msg;
    panel.status.dataset.kind = kind;
  };

  const user = readUser();
  if (user && user.email) {
    const who = panel.root.shadowRoot.querySelector(".who");
    if (who) who.textContent = user.email.split("@")[0];
  }
  if (!user?.email) {
    return say("Sign in on this page first, then run the solver again.", "bad");
  }
  if (!/tds-\d{4}-\d{2}-ga8/.test(location.pathname)) {
    say("This does not look like the GA8 page — filling what I can anyway.", "warn");
  }

  for (const key of DEAD_KEYS) {
    try { localStorage.removeItem(key); } catch { /* private mode */ }
  }

  let result;
  try {
    result = solveEmail(user.email);
  } catch (error) {
    return say(`Could not derive your answers: ${error.message}`, "bad");
  }

  // Q8 and Q9 take a JSON value; Q1-Q7 take the service that answers for you.
  let filled = 0;
  for (const key of ["q8", "q9"]) {
    if (setField(QUESTION_IDS[key], JSON.stringify(result[key].answer, null, 2))) filled++;
  }

  const url = serviceUrlFor(user.email);
  for (const [id] of SERVER_IDS) if (setField(id, url)) filled++;

  // Q10 is the one answer that lives outside this page: the box takes a Hugging
  // Face repo URL and the grader reads the emissions out of that repo's README.
  // Whatever is already in the box -- an earlier attempt of your own, a repo the
  // page remembered from a previous save -- is a URL whose *contents* this cannot
  // see, and those figures are per-student. So none of it is trusted. Publishing
  // is idempotent, which makes doing it every run both the cheap move and the only
  // one that guarantees the repo holds this student's numbers.
  say("Publishing your carbon card…");
  const published = await publishCard(url, result.q10.modelCard);
  const q10 = published.url || cachedRepo(user.email);
  let q10problem = published.url ? "" : published.error;
  if (published.url) rememberRepo(user.email, published.url);
  if (q10 && setField(QUESTION_IDS.q10, q10)) filled++;

  // A repo URL sitting in the box proves nothing: the grader is the only thing
  // that can say whether that repo answers Q10 for this student. Ask it now, so a
  // wrong URL is caught here rather than after the marks are in.
  let q10ok = false;
  if (q10) {
    say("Checking Q10 with the grader…");
    const check = await gradeOne(Q10_ROW);
    q10ok = check.score >= Q10_ROW[2];
    if (!q10ok) q10problem = check.message || "the grader did not accept that repo";
  }

  const allGood = filled === 10 && q10ok;
  say(allGood ? "All 10 answers filled." : `${filled} of 10 filled — the rest are marked below.`,
      allGood ? "good" : "warn");

  // Show what went into each box. Nobody has to act on it, but a number you
  // cannot see is a number you cannot sanity-check before saving.
  panel.body.append(answerRow("Q1–Q7", "servers", url, true));
  panel.body.append(answerRow("Q8", "LoRA budget", JSON.stringify(result.q8.answer, null, 2), true));
  panel.body.append(answerRow("Q9", "training fingerprint", JSON.stringify(result.q9.answer, null, 2), true));
  panel.body.append(answerRow("Q10", "carbon card", q10 || "not published", q10ok));
  if (!q10ok) panel.body.append(carbonFallback(result.q10.modelCard, q10problem));

  panel.body.append(saveRow(panel, say));
}

/* ---------------------------------------------------------------- exam page */

function isHfUrl(v) {
  return HF_RE.test((v || "").trim());
}

function fieldValue(id) {
  const f = document.querySelector(`[name="${CSS.escape(id)}"]`);
  return (f && f.value) || "";
}

function readUser() {
  try {
    return JSON.parse(localStorage.getItem("user") || "null");
  } catch {
    return null;
  }
}

/** The page persists on input, so dispatching the event is what makes it stick. */
function setField(questionId, value) {
  const field = document.querySelector(`[name="${CSS.escape(questionId)}"]`);
  if (!field) return false;
  field.value = value;
  field.dispatchEvent(new Event("input", { bubbles: true }));
  field.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}

/* -------------------------------------------------------------------- Q10 */

function cachedRepo(email) {
  try {
    const v = localStorage.getItem(repoKey(email));
    return isHfUrl(v) ? v.trim() : null;
  } catch {
    return null;
  }
}

function rememberRepo(email, url) {
  try { localStorage.setItem(repoKey(email), url); } catch { /* private mode */ }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Ask the service that answers Q1-Q7 to publish the card too.
 *  Returns {url} or {error} -- never a bare null, because "it did not work" with
 *  no reason attached is exactly what left students staring at an empty Q10. */
async function publishCard(serviceUrl, card) {
  const endpoint = serviceUrl.replace(/\/+$/, "") + "/carbon-repo";
  let reason = "no response";

  // Hugging Face wobbling and a cold service both fail in ways that come right on
  // a second try, so a transient error is retried rather than reported.
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt) await sleep(800 * attempt);
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && isHfUrl(data.url)) return { url: data.url };
      reason = data.detail || data.error || `HTTP ${res.status}`;
      // A refused card, or a service with no account attached, will be refused
      // again a second later. Only a server-side wobble is worth another go.
      if (res.status < 500 || res.status === 503) break;
    } catch (err) {
      reason = err.message || "the request did not reach the service";
    }
  }

  console.warn("[GA8 solver] could not publish the carbon card:", reason);
  return { error: reason };
}

/** When publishing is not available, Q10 is still a minute of work rather than a
 *  lost 2.5 -- so hand over the card and the steps, instead of a dead end. */
function carbonFallback(card, reason) {
  const row = el("div", "row");
  row.append(el("h4", null, "Q10 — publish it yourself, about a minute"));
  if (reason) row.append(el("div", "note", reason));

  row.append(el("div", "note",
    "1. Copy the card below. 2. Create a public model repo. 3. Paste the card in as " +
    "its README.md and commit. 4. Put the repo URL in the Q10 box, then Save."));

  const link = el("a", null, "huggingface.co/new");
  link.href = "https://huggingface.co/new";
  link.target = "_blank";
  link.rel = "noopener";
  const linkLine = el("div", "note");
  linkLine.append(link);
  row.append(linkLine);

  row.append(el("div", "val", card));

  const copy = el("button", "go", "Copy the card");
  copy.type = "button";
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(card);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Select the card above and copy it";
    }
    setTimeout(() => (copy.textContent = "Copy the card"), 2500);
  };
  row.append(copy);
  return row;
}

/* ------------------------------------------------------------------- panel */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function mountPanel() {
  document.getElementById(PANEL_ID)?.remove();

  const root = el("div");
  root.id = PANEL_ID;
  root.attachShadow({ mode: "open" });

  const style = el("style");
  style.textContent = `
    :host { all: initial; }
    .card {
      position: fixed; right: 16px; bottom: 16px; width: 380px; max-height: 78vh;
      overflow: auto; z-index: 2147483647;
      background: #141b1a; color: #e4eae7; border: 1px solid #35443f; border-radius: 10px;
      font: 12.5px/1.55 ui-monospace, Consolas, monospace;
      box-shadow: 0 10px 40px rgba(0,0,0,.45);
    }
    .head { display:flex; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid #253130; }
    .head b { font-size: 13px; }
    .head .x { margin-left:auto; cursor:pointer; color:#7a8884; background:none; border:0; font:inherit; }
    .status { padding:8px 12px; color:#a3b1ad; border-bottom:1px solid #253130; }
    .status[data-kind=good] { color:#6fc9ad; }
    .status[data-kind=warn] { color:#d6a54e; }
    .status[data-kind=bad]  { color:#e08a78; }
    .body { padding: 4px 12px 12px; }
    .row { padding:9px 0; border-bottom:1px solid #253130; }
    .row:last-child { border-bottom:0; }
    .row h4 { margin:0 0 4px; font-size:12.5px; display:flex; gap:8px; align-items:center; }
    .tick { color:#6fc9ad; } .cross { color:#e08a78; }
    .kv { color:#7a8884; font-size:11.5px; }
    .val { margin-top:5px; padding:6px 8px; background:#0d1211; border:1px solid #253130;
           border-radius:6px; white-space:pre-wrap; word-break:break-all; font-size:11.5px;
           max-height:190px; overflow:auto; }
    input { width:100%; padding:7px 8px; margin-top:6px; background:#0d1211; color:#e4eae7;
            border:1px solid #35443f; border-radius:6px; font:inherit; }
    button.go { margin-top:8px; padding:7px 12px; background:#6fc9ad; color:#0d1211;
                border:0; border-radius:6px; font:inherit; font-weight:600; cursor:pointer; }
    button.go[disabled] { opacity:.5; cursor:default; }
    a { color:#6fc9ad; }
    .note { color:#7a8884; font-size:11.5px; margin-top:6px; }
    .who { color:#7a8884; font-size:11.5px; }
    .score { margin-top:10px; text-align:center; font-size:22px; font-weight:700; color:#a3b1ad; }
    .score[data-full=yes] { color:#6fc9ad; }
    .marks { display:flex; flex-wrap:wrap; gap:4px; margin-top:8px; justify-content:center; }
    .mark { font-size:10.5px; padding:2px 6px; border-radius:4px;
            background:#0d1211; border:1px solid #253130; color:#7a8884; }
    .mark[data-ok=yes] { color:#6fc9ad; border-color:#2f4a42; }
    .mark[data-ok=no]  { color:#e08a78; border-color:#4a2f2b; }
  `;

  const card = el("div", "card");
  const head = el("div", "head");
  head.append(el("b", null, "GA8 Solver"));
  head.append(el("span", "who", ""));
  const close = el("button", "x", "close");
  close.type = "button";
  close.onclick = () => root.remove();
  head.append(close);

  const status = el("div", "status", "Working…");
  const body = el("div", "body");
  card.append(head, status, body);
  root.shadowRoot.append(style, card);
  document.body.append(root);

  return { root, status, body };
}

/** Ask the exam's own marker what each answer is worth. Same origin, and the
 *  signature it needs is already in this page's storage, so this is the real
 *  score rather than a guess at one. Returns the score and, when it falls short,
 *  whatever the marker said about why. */
async function gradeOne([id, , weight, versioned]) {
  const user = readUser();
  const body = {
    email: user.email,
    quizSign: user.quizSign,
    response: fieldValue(id),
    weight,
    questionId: id,
  };
  if (versioned) body.version = "v1";
  try {
    const res = await fetch("/backendVerify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return {
      score: typeof data.score === "number" ? data.score : 0,
      message: data.message || data.error || data.reason || "",
    };
  } catch (err) {
    return { score: 0, message: err.message || "the marker did not answer" };
  }
}

function saveRow(panel, say) {
  const row = el("div", "row");

  const go = el("button", "go", "Save all answers");
  go.type = "button";
  go.style.width = "100%";
  go.style.padding = "10px";
  go.style.fontSize = "13px";
  row.append(go);

  const out = el("div", "score", "");
  row.append(out);
  const detail = el("div", "marks");
  row.append(detail);

  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "Saving…";

    const button = [...document.querySelectorAll("button")]
      .find((b) => /^\s*save\s*$/i.test(b.textContent || ""));
    if (button) button.click();

    detail.textContent = "";
    out.textContent = "0 / " + TOTAL;
    let got = 0;

    // Graded one at a time so the running total actually moves, and so ten
    // gradings do not hit the single shared instance at once.
    for (const q of WEIGHTS) {
      const chip = el("span", "mark", q[1] + " …");
      detail.append(chip);
      const { score } = await gradeOne(q);
      got += score;
      chip.textContent = `${q[1]} ${round2(score)}`;
      chip.dataset.ok = score >= q[2] ? "yes" : "no";
      out.textContent = `${round2(got)} / ${TOTAL}`;
      out.dataset.full = round2(got) === TOTAL ? "yes" : "no";
    }

    go.disabled = false;
    go.textContent = "Save again";
    say(round2(got) === TOTAL ? "Full marks, saved." : "Saved — see the breakdown.", "good");
  };

  return row;
}

function round2(n) {
  return Math.round(n * 100) / 100;
}

function answerRow(label, what, value, ok) {
  const row = el("div", "row");
  const h = el("h4");
  h.append(el("span", ok ? "tick" : "cross", ok ? "✓" : "✗"));
  h.append(el("span", null, label));
  h.append(el("span", "kv", what));
  row.append(h);
  row.append(el("div", "val", value));
  return row;
}

// Last line on purpose. Called from the top of the file, this module's own
// constants are still in their dead zone while main() runs, and that crash has
// cost two releases already -- once for HF_RE, once for the WEIGHTS table.
main();
