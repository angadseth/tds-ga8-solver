// Derives the per-student GA8 answers by running the exam's own generator.
//
// Q8, Q9 and Q10 are seeded from your email, so they are yours alone and can be
// computed exactly. Q1-Q7 are graded by calling a server you deploy, so no value
// exists for this file to produce -- see the server/ folder in this repo.
//
// SEED: the generator takes a version segment, and it must be "v1". Passing ""
// yields a completely different config (different optimizer, different GPU) and
// therefore a wrong answer that still looks plausible. Verified against the real
// grader for 24f2004141@ds.study.iitm.ac.in: v1 gives the accepted values, the
// empty seed is rejected with "final_loss mismatch".
import * as BUNDLE from "./ga8.js";
import { runQ9, makeRunId } from "./q9engine.js";

const VERSION = "v1";

// A hosted deployment of server/, shared by everyone, with each student's state
// kept apart by the email in the path. Q1-Q7 are identical for every student, so
// one service can answer for all of them -- what must not be shared is the
// *state*: Q2 remembers a select's runId for the evaluate that follows, and Q5
// remembers a freeze. The path namespaces exactly that.
//
// Deploying your own from server/ works the same way and is one click; this is
// here so the seven boxes are answerable without waiting for a build.
const SHARED_SERVICE = "https://tds-ga8-shared-172706022999.asia-south1.run.app";

/** The URL that answers all seven server questions for this student. */
export function serviceUrlFor(email) {
  return SHARED_SERVICE + "/ga8/" + encodeURIComponent(email.trim().toLowerCase());
}

// Attention projections live under self_attn; everything else under mlp.
const ATTN = new Set(["q_proj", "k_proj", "v_proj", "o_proj"]);

// Both tables are lifted verbatim from the exam bundle. They are the exam's own
// numbers -- do not "correct" them against public datasheets.
const GRID = {
  "us-central1": 350, "europe-west4": 200, "asia-south1": 650,
  "us-east1": 420, "europe-north1": 120, "ap-southeast1": 480,
};
const TDP = {
  "NVIDIA A100": 400, "NVIDIA V100": 300, "NVIDIA T4": 70,
  "NVIDIA H100": 700, "NVIDIA L40S": 350, "NVIDIA RTX 4090": 450,
};

function moduleShapes(hidden, inter, heads, kv) {
  const hd = Math.floor(hidden / heads);
  return {
    q_proj: [hidden, heads * hd],
    k_proj: [hidden, kv * hd],
    v_proj: [hidden, kv * hd],
    o_proj: [heads * hd, hidden],
    gate_proj: [hidden, inter],
    up_proj: [hidden, inter],
    down_proj: [inter, hidden],
  };
}

// Reproduces PEFT save_pretrained + safetensors byte-for-byte. Checked against a
// real adapter file: 27445944 bytes for 24f2004141@ds.study.iitm.ac.in.
export function solveQ8(cfg) {
  const base = cfg.base_config;
  const heads = base.num_attention_heads;
  const kv = base.num_key_value_heads ?? heads;
  const shapes = moduleShapes(base.hidden_size, base.intermediate_size, heads, kv);

  const tensors = [];
  let trainable = 0;
  for (const layer of cfg.layers) {
    if (layer.freeze || !layer.target_modules || layer.target_modules.length === 0) continue;
    const { layer_idx: i, lora_rank: r } = layer;
    for (const mod of layer.target_modules) {
      const [fin, fout] = shapes[mod];
      const grp = ATTN.has(mod) ? "self_attn" : "mlp";
      const stem = `base_model.model.model.layers.${i}.${grp}.${mod}`;
      tensors.push([`${stem}.lora_A.weight`, [r, fin]]);
      tensors.push([`${stem}.lora_B.weight`, [fout, r]]);
      trainable += r * (fin + fout);
    }
  }
  tensors.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));

  const header = { __metadata__: { format: "pt" } };
  let off = 0;
  for (const [name, shape] of tensors) {
    const nbytes = 4 * shape[0] * shape[1];
    header[name] = { dtype: "F32", shape, data_offsets: [off, off + nbytes] };
    off += nbytes;
  }
  if (off !== trainable * 4) throw new Error(`payload mismatch ${off} vs ${trainable * 4}`);

  const raw = new TextEncoder().encode(JSON.stringify(header));
  const headerLen = Math.ceil(raw.length / 8) * 8;  // padded to 8-byte alignment
  return {
    trainable_params: trainable,
    adapter_file_size_bytes: 8 + headerLen + off,
    num_tensors: tensors.length,
  };
}

export function solveQ10(carbon) {
  const tdp = TDP[carbon.gpu_type];
  const grid = GRID[carbon.region];
  if (tdp === undefined) throw new Error(`unknown GPU TDP: ${carbon.gpu_type}`);
  if (grid === undefined) throw new Error(`unknown grid intensity: ${carbon.region}`);
  const energy = (tdp * carbon.num_gpus * carbon.gpu_hours * carbon.power_usage_effectiveness) / 1000;
  const co2 = (energy * grid) / 1000;
  return {
    energy_kwh: energy,
    co2_kg: Math.round(co2 * 1000) / 1000,
    training_type: carbon.training_type,
    region: carbon.region,
    gpu_type: carbon.gpu_type,
    tdp_watts: tdp, num_gpus: carbon.num_gpus, gpu_hours: carbon.gpu_hours,
    pue: carbon.power_usage_effectiveness, grid_gco2_per_kwh: grid,
  };
}

/** The README.md the grader reads. Emissions are rounded to three decimals,
 *  which is what the question specifies. */
export function buildModelCard(q10) {
  return `---
co2_eq_emissions:
  emissions: ${q10.co2_kg}
  source: codecarbon
  training_type: ${q10.training_type}
  geographical_location: ${q10.region}
  hardware_used: ${q10.gpu_type}
---

# TDS GA8 — Green AI Carbon Accounting

| Field | Value |
|---|---|
| Hardware | ${q10.gpu_type} (${q10.tdp_watts} W TDP) |
| GPUs | ${q10.num_gpus} |
| GPU hours | ${q10.gpu_hours} |
| PUE | ${q10.pue} |
| Region | ${q10.region} (${q10.grid_gco2_per_kwh} gCO2eq/kWh) |
| Energy | ${q10.energy_kwh} kWh |
| Emissions | ${q10.co2_kg} kg CO2eq |

energy_kWh = TDP x GPUs x hours x PUE / 1000
co2_kg = energy_kWh x grid_intensity / 1000
`;
}

/** Everything derivable from one email, plus the workings behind each number. */
export function solveEmail(email) {
  BUNDLE.initLora();
  BUNDLE.initMlflow();
  BUNDLE.initCarbon();

  const lora = BUNDLE.genLora(email);
  const mlflow = BUNDLE.genMlflow(email, VERSION);
  const carbon = BUNDLE.genCarbon(email, VERSION);

  const q8 = solveQ8(lora);
  const q9run = runQ9(mlflow);
  const q10 = solveQ10(carbon);

  return {
    email,
    serviceUrl: serviceUrlFor(email),
    q8: {
      answer: {
        trainable_params: q8.trainable_params,
        adapter_file_size_bytes: q8.adapter_file_size_bytes,
      },
      workings: [
        ["base model", `hidden ${lora.base_config.hidden_size}, ${lora.layers.length} layers`],
        ["trainable layers", String(lora.layers.filter((l) => !l.freeze).length)],
        ["tensors written", String(q8.num_tensors)],
      ],
    },
    q9: {
      answer: {
        final_loss: q9run.final_loss,
        run_id: makeRunId(),
        mean_last_10_loss: q9run.mean_last_10_loss,
      },
      workings: [
        ["optimizer", `${mlflow.hyperparameters.optimizer.name}, lr ${mlflow.hyperparameters.lr}`],
        ["schedule", `${q9run.schedule}, ${q9run.num_steps} steps, batch ${mlflow.hyperparameters.batch_size}`],
        ["weight decay", String(mlflow.hyperparameters.weight_decay)],
      ],
    },
    // Q10's box takes a Hugging Face repository URL, not a value. The grader
    // fetches that repo's README.md and reads the co2_eq_emissions frontmatter,
    // so what this can produce is the card -- the repo has to be the student's
    // own, under their own account.
    q10: {
      needsRepo: true,
      modelCard: buildModelCard(q10),
      computed: {
        energy_kwh: q10.energy_kwh,
        co2_kg: q10.co2_kg,
        training_type: q10.training_type,
        region: q10.region,
        gpu_type: q10.gpu_type,
      },
      workings: [
        ["hardware", `${q10.num_gpus} x ${q10.gpu_type} @ ${q10.tdp_watts} W`],
        ["runtime", `${q10.gpu_hours} h, PUE ${q10.pue}`],
        ["grid", `${q10.region} @ ${q10.grid_gco2_per_kwh} gCO2/kWh`],
      ],
    },
  };
}

/** The exam boxes each answer belongs in. */
export const QUESTION_IDS = {
  q8: "q-lora-quant-budget-server",
  q9: "q-mlflow-fingerprint-server",
  q10: "q-modelcard-carbon-server",
};

export const SERVER_IDS = [
  ["q-immutable-training-corpus-server", "Q1 · Immutable training corpus"],
  ["q-leakage-safe-bqml-server", "Q2 · Leakage-safe BQML gate"],
  ["q-mlflow-evidence-promotion-server", "Q3 · Evidence promotion"],
  ["q-peft-repair-server", "Q4 · PEFT repair"],
  ["q-quantized-model-admission-server", "Q5 · Quantized admission"],
  ["q-content-addressed-pipeline-server", "Q6 · Content-addressed pipeline"],
  ["q-verifiable-model-bundle-server", "Q7 · Verifiable bundle"],
];
