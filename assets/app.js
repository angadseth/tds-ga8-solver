// The page you are reading: takes an email, shows the three derived answers and
// the working behind each. Everything runs here in the tab; no request leaves it.
import { solveEmail, QUESTION_IDS, SERVER_IDS } from "./engine.js";

const $ = (sel, root = document) => root.querySelector(sel);

const form = $("#run");
const emailInput = $("#email");
const status = $("#status");
const results = $("#results");

const say = (msg, kind = "idle") => {
  status.textContent = msg;
  status.dataset.kind = kind;
};

// Remember the email so a reload does not mean retyping it.
const REMEMBERED = "ga8-solver-email";
emailInput.value = localStorage.getItem(REMEMBERED) || "";

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const email = emailInput.value.trim();
  if (!email) return say("Enter your IITM email to begin", "idle");

  say(`Deriving your variant from ${email}…`, "idle");
  let result;
  const started = performance.now();
  try {
    result = solveEmail(email);
  } catch (error) {
    return say(`Could not derive answers: ${error.message}`, "bad");
  }
  localStorage.setItem(REMEMBERED, email);

  render(result);
  say(
    `Three answers derived in ${Math.round(performance.now() - started)} ms — the working is shown under each.`,
    "good"
  );
});

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

const TITLES = {
  q8: ["Q8", "LoRA parameter & adapter budget", QUESTION_IDS.q8],
  q9: ["Q9", "PyTorch training-loop fingerprint", QUESTION_IDS.q9],
  q10: ["Q10", "Model card carbon audit", QUESTION_IDS.q10],
};

function render(result) {
  results.hidden = false;
  results.textContent = "";
  results.append(el("h2", "ga8-h", `Your answers · ${result.email}`));

  for (const key of ["q8", "q9", "q10"]) {
    const [num, title, questionId] = TITLES[key];
    const entry = result[key];
    const card = el("article", "ga8-card");

    const head = el("h3", "ga8-head");
    head.append(el("span", "ga8-num", num));
    head.append(el("span", null, title));
    card.append(head);
    card.append(el("p", "ga8-qid", questionId));

    const works = el("ul", "ga8-works");
    for (const [k, v] of entry.workings) {
      const li = el("li");
      li.append(el("b", null, k));
      li.append(el("span", null, v));
      works.append(li);
    }
    card.append(works);

    const text = JSON.stringify(entry.answer, null, 2);
    card.append(el("pre", "ga8-json", text));

    const copy = el("button", "ga8-copy", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(text);
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy"), 1500);
    });
    card.append(copy);
    results.append(card);
  }

  // The other seven are endpoints, so say plainly that there is nothing to copy.
  const note = el("article", "ga8-card");
  note.append(el("h3", "ga8-head", "Q1–Q7 · seven servers"));
  note.append(el("p", "ga8-qid",
    "Graded by calling a URL, so there is no value to derive. Deploy the code in this repo's server/ folder and paste the URL it gives you into these seven boxes."));
  const list = el("ul", "ga8-ids");
  for (const [id, label] of SERVER_IDS) {
    const li = el("li");
    li.append(el("span", null, label));
    li.append(el("code", null, id));
    list.append(li);
  }
  note.append(list);
  results.append(note);
}
