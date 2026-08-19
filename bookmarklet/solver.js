/**
 * Runs on the GA8 exam page itself.
 *
 * Same-origin, so it can read the email you are signed in as, derive your Q8/Q9/Q10
 * answers in this tab, and type them into the boxes. Nothing is sent anywhere.
 *
 * Q1-Q7 are graded by calling a server, so there is no value to fill: paste your
 * own deployed URL once and it goes into all seven boxes.
 *
 * Saving is a button rather than something that happens on load -- it overwrites
 * whatever you submitted before, so it should be your decision, made after you
 * have looked at what got filled in.
 */
import { solveEmail, serviceUrlFor, QUESTION_IDS, SERVER_IDS } from "../assets/engine.js";

const PANEL_ID = "ga8-solver-panel";
const URL_KEY = "ga8-solver-service-url";

main();

async function main() {
  const panel = mountPanel();
  const say = (msg, kind = "") => {
    panel.status.textContent = msg;
    panel.status.dataset.kind = kind;
  };

  const user = readUser();
  if (!user?.email) {
    return say("Sign in on this page first, then run the solver again.", "bad");
  }
  if (!/tds-\d{4}-\d{2}-ga8/.test(location.pathname)) {
    say("This does not look like the GA8 page — filling what I can anyway.", "warn");
  }

  let result;
  try {
    result = solveEmail(user.email);
  } catch (error) {
    return say(`Could not derive your answers: ${error.message}`, "bad");
  }

  let filled = 0;
  for (const key of ["q8", "q9", "q10"]) {
    const text = JSON.stringify(result[key].answer, null, 2);
    const ok = setField(QUESTION_IDS[key], text);
    if (ok) filled++;
    panel.body.append(answerRow(key.toUpperCase(), result[key], ok));
  }

  // The seven server boxes take a URL, and the shared deployment answers for
  // this student at their own path -- so fill them straight away rather than
  // making them paste something first.
  const stored = localStorage.getItem(URL_KEY);
  const url = stored || serviceUrlFor(user.email);
  for (const [id] of SERVER_IDS) if (setField(id, url)) filled++;

  say(
    `${filled} of 10 answers filled for ${user.email}`,
    filled === 10 ? "good" : "warn"
  );
  panel.body.append(serviceRow(panel, say, url, !stored));
  panel.body.append(saveRow(panel, say));
}

/* ---------------------------------------------------------------- exam page */

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
           border-radius:6px; white-space:pre-wrap; word-break:break-all; font-size:11.5px; }
    input { width:100%; padding:7px 8px; margin-top:6px; background:#0d1211; color:#e4eae7;
            border:1px solid #35443f; border-radius:6px; font:inherit; }
    button.go { margin-top:8px; padding:7px 12px; background:#6fc9ad; color:#0d1211;
                border:0; border-radius:6px; font:inherit; font-weight:600; cursor:pointer; }
    button.go[disabled] { opacity:.5; cursor:default; }
    .note { color:#7a8884; font-size:11.5px; margin-top:6px; }
  `;

  const card = el("div", "card");
  const head = el("div", "head");
  head.append(el("b", null, "GA8 Solver"));
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

function answerRow(label, entry, filled) {
  const row = el("div", "row");
  const h = el("h4");
  h.append(el("span", filled ? "tick" : "cross", filled ? "✓" : "✗"));
  h.append(el("span", null, label));
  row.append(h);
  for (const [k, v] of entry.workings) row.append(el("div", "kv", `${k}: ${v}`));
  row.append(el("div", "val", JSON.stringify(entry.answer, null, 2)));
  if (!filled) row.append(el("div", "note", "Box not found on this page — copy the value in by hand."));
  return row;
}

function serviceRow(panel, say, current, isShared) {
  const row = el("div", "row");
  const h = el("h4");
  h.append(el("span", "tick", "✓"));
  h.append(el("span", null, "Q1–Q7 · server URL"));
  row.append(h);
  row.append(el("div", "kv", isShared
    ? "Filled with the shared deployment, on your own path so your grading state stays yours. Running your own copy instead? Paste its URL and press the button."
    : "Filled with the URL you saved earlier."));

  const input = el("input");
  input.type = "url";
  input.placeholder = "https://your-service-xxxxx.run.app";
  input.value = current || "";
  row.append(input);

  const go = el("button", "go", "Fill all seven");
  go.type = "button";
  go.onclick = () => {
    const url = input.value.trim().replace(/\/+$/, "");
    if (!/^https:\/\/.+/.test(url)) return say("That does not look like an https URL.", "bad");
    localStorage.setItem(URL_KEY, url);
    let n = 0;
    for (const [id] of SERVER_IDS) if (setField(id, url)) n++;
    say(`${n} of 7 server boxes filled with your URL.`, n === 7 ? "good" : "warn");
  };
  row.append(go);
  return row;
}

function saveRow(panel, say) {
  const row = el("div", "row");
  row.append(el("h4", null, "Save"));
  row.append(el("div", "kv", "This overwrites your previous submission. Look over what got filled in first."));

  const go = el("button", "go", "Press Save on this page");
  go.type = "button";
  go.onclick = () => {
    const button = [...document.querySelectorAll("button")]
      .find((b) => /^\s*save\s*$/i.test(b.textContent || ""));
    if (!button) return say("Could not find the Save button — press it yourself.", "warn");
    button.click();
    say("Save pressed. Check the score the page reports.", "good");
  };
  row.append(go);
  return row;
}
