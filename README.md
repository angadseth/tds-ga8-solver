# GA8 Solver — derived, not guessed

**→ [angadseth.github.io/tds-ga8-solver](https://angadseth.github.io/tds-ga8-solver/)**

Enter your IITM email; it derives your Q8, Q9 and Q10 by running the exam's own question
generator, in your tab, and shows the working behind each number. Nothing is uploaded — there is
no backend to upload to.

## Why the answers are exact, not approximate

Three of the ten questions are seeded from your email, so each student's answer is different.
This does not guess at them; it runs the generator the exam itself ships and then does the real
computation on the config it produces:

- **Q8 — LoRA budget.** Rebuilds the PEFT adapter's safetensors layout tensor by tensor,
  including the 8-byte-aligned JSON header, so the file size is *counted*, not estimated.
  Checked against a real adapter file: 27,445,944 bytes, to the byte.
- **Q9 — Training fingerprint.** Replays the entire training loop in float64. The generator picks
  SGD, AdamW or RMSprop per student, on a cosine or step schedule, so all six combinations are
  implemented — including the detail that `torch.optim.SGD` seeds its momentum buffer with the
  first gradient rather than with zeros. Agreement with a real PyTorch float32 run: ~7e-8, against
  a grader tolerance of 1e-3.
- **Q10 — Carbon audit.** Energy and CO₂ from the exam's own TDP and grid-intensity tables, which
  do not always match the public datasheets. Using the real-world numbers gives a wrong answer.
  Note that Q10's box takes a **Hugging Face repository URL**, not a value: the grader fetches
  that repo's `README.md` and reads the `co2_eq_emissions` frontmatter. So the solver writes the
  card for you and the repo is yours to create — the only step it cannot do on your behalf.

All three are checked against answers the real grader has accepted before shipping.

## What it cannot do, honestly

**Q1–Q7 are servers.** They grade a URL by posting a fixed set of requests to it and scoring the
replies. No web page can produce that for you, and using someone else's URL means it is their
service being graded, not yours.

So this repo ships [`server/`](./server) instead — all seven handlers, full marks on every one,
plus one deploy command. The URL that comes back is yours. `server/README.md` also lists the
mistakes that cost marks along the way, which is the part worth reading.

## Layout

```
index.html            the page
assets/engine.js     Q8 and Q10 math, and the per-email orchestration
assets/q9engine.js   the training loop — SGD / AdamW / RMSprop, cosine / step
assets/ga8.js        the exam's own generator, vendored unchanged
shims/                three stubs so the generator loads outside the exam page
bookmarklet/solver.js runs on the exam page: fills the boxes, offers Save
server/               the Q1–Q7 service, to deploy as your own
```

## The one thing to get right if you fork this

The generator takes a version segment and it must be **`"v1"`**. Passing `""` produces a
completely different config — a different optimizer, a different GPU — and therefore an answer
that looks entirely plausible and is wrong. Both were put in front of the real grader: `v1` is
accepted, the empty seed comes back `final_loss mismatch`.

---

Independent student project by [Angad Jangir](https://github.com/angadseth). Not affiliated with,
endorsed by, or representing IIT Madras or the TDS course team. Built for the graded assignments,
whose instructions permit any help you can find; not for the proctored exams, where it must not
be used.
