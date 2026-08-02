// MAC Web UI — frontend logic.
// Submits the config form, streams SSE progress, loads the GLB into <model-viewer>.

const STAGES = [
  { prefix: "SPEC_PLANNER", label: "1. Spec Planner" },
  { prefix: "ARCHITECT",    label: "2. Geometric Architect" },
  { prefix: "CODER",        label: "3. Python Coder" },
  { prefix: "REPAIR",       label: "4. Aider Repair (fallback)" },
];

let providers = {};

async function loadSchema() {
  const r = await fetch("/api/config/schema");
  const d = await r.json();
  const cfg = d.config;
  providers = d.providers;

  document.getElementById("DS_BASE_URL").value = cfg.DS_BASE_URL;
  document.getElementById("prompt").value = cfg.USER_REQUEST;
  document.getElementById("workflow").value = cfg.WORKFLOW_ID || "original";
  document.getElementById("MAX_RETRIES").value = cfg.MAX_RETRIES;
  document.getElementById("MAX_EXEC_RETRIES").value = cfg.MAX_EXEC_RETRIES;
  document.getElementById("provider").value = "qwen";

  const tbody = document.querySelector("#stage-table tbody");
  tbody.innerHTML = "";
  for (const s of STAGES) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.label}</td>
      <td><input type="text" id="${s.prefix}_MODEL" value="${cfg[s.prefix + "_MODEL"] ?? ""}" /></td>
      <td><input type="number" step="0.1" id="${s.prefix}_TEMPERATURE" value="${cfg[s.prefix + "_TEMPERATURE"] ?? 0}" /></td>
      <td><input type="number" id="${s.prefix}_MAX_TOKENS" value="${cfg[s.prefix + "_MAX_TOKENS"] ?? 0}" /></td>`;
    tbody.appendChild(tr);
  }

  // Mark model inputs as customized once the user edits them (so provider
  // preset auto-fill doesn't clobber their choice).
  for (const s of STAGES) {
    const inp = document.getElementById(s.prefix + "_MODEL");
    inp.addEventListener("input", () => { inp.dataset.customized = "1"; });
  }
}

document.getElementById("provider").addEventListener("change", (e) => {
  const p = providers[e.target.value];
  if (!p) return;
  document.getElementById("DS_BASE_URL").value = p.ds_base_url;
  for (const s of STAGES) {
    const inp = document.getElementById(s.prefix + "_MODEL");
    if (inp && !inp.dataset.customized) inp.value = p.model_hint;
  }
});

document.getElementById("run-btn").addEventListener("click", async () => {
  const config = {
    DS_BASE_URL: document.getElementById("DS_BASE_URL").value,
    MAX_RETRIES: parseInt(document.getElementById("MAX_RETRIES").value, 10),
    MAX_EXEC_RETRIES: parseInt(document.getElementById("MAX_EXEC_RETRIES").value, 10),
  };
  for (const s of STAGES) {
    config[s.prefix + "_MODEL"] = document.getElementById(s.prefix + "_MODEL").value;
    config[s.prefix + "_TEMPERATURE"] = parseFloat(document.getElementById(s.prefix + "_TEMPERATURE").value);
    config[s.prefix + "_MAX_TOKENS"] = parseInt(document.getElementById(s.prefix + "_MAX_TOKENS").value, 10);
  }

  const body = {
    config,
    prompt: document.getElementById("prompt").value,
    api_key: document.getElementById("api_key").value,
    workflow: document.getElementById("workflow").value,
    dest_path: document.getElementById("dest_path").value,
  };

  const log = document.getElementById("log");
  const status = document.getElementById("status");
  const mv = document.getElementById("mv");
  log.textContent = "";
  status.textContent = "Submitting...";
  document.getElementById("downloads").innerHTML = "";
  document.getElementById("stats").textContent = "";
  mv.removeAttribute("src");

  const r = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const t = await r.text();
    status.textContent = "Error: " + t;
    return;
  }
  const { job_id } = await r.json();
  streamEvents(job_id);
});

function streamEvents(jobId) {
  const log = document.getElementById("log");
  const status = document.getElementById("status");
  const mv = document.getElementById("mv");
  const es = new EventSource(`/api/jobs/${jobId}/events`);

  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.log) {
      log.textContent += msg.log + "\n";
      log.scrollTop = log.scrollHeight;
    }
    if (msg.stage) {
      status.textContent = `Stage: ${msg.stage} (iter ${msg.iter})`;
    }
    if (msg.warn) {
      log.textContent += "⚠ " + msg.warn + "\n";
    }
    if (msg.intermediate) {
      mv.setAttribute("src", msg.glb);
      status.textContent = "Live: intermediate model updated";
    }
    if (msg.done) {
      status.textContent = `Done — error_type: ${msg.error_type} · tokens: ${msg.tokens} · API calls: ${msg.api_calls}`;
      es.close();
      showResult(jobId, msg);
    }
    if (msg.error) {
      status.textContent = "Error: " + msg.error;
      log.textContent += "✗ " + msg.error + "\n";
      es.close();
    }
  };

  es.onerror = () => {
    status.textContent = "Connection lost.";
    es.close();
  };
}

function showResult(jobId, msg) {
  const mv = document.getElementById("mv");
  if (msg.glb) {
    mv.setAttribute("src", `/api/jobs/${jobId}/files/model.glb`);
  } else {
    mv.setAttribute("alt", "No GLB available — try downloading the STEP/STL");
  }

  const dl = document.getElementById("downloads");
  dl.innerHTML = "";
  const files = [
    ["STEP", "model.step"],
    ["STL", "model.stl"],
    ["Python source", "source.py"],
    ["Measurements", "measurements.json"],
    ["Runtime diagnostics", "missed.json"],
  ];
  for (const [label, fname] of files) {
    const a = document.createElement("a");
    a.href = `/api/jobs/${jobId}/files/${fname}`;
    a.textContent = `⬇ ${label}`;
    a.className = "dl-btn";
    dl.appendChild(a);
  }

  document.getElementById("stats").textContent =
    `Tokens: ${msg.tokens} · API calls: ${msg.api_calls}`;
}

loadSchema();
