// MAC Web UI — frontend logic.
// Submits the config form, streams SSE progress, loads the GLB into <model-viewer>.

const STAGES = [
  { prefix: "SPEC_PLANNER", label: "1. Spec Planner" },
  { prefix: "ARCHITECT",    label: "2. Geometric Architect" },
  { prefix: "CODER",        label: "3. Python Coder" },
  { prefix: "REPAIR",       label: "4. Aider Repair (fallback)" },
];

let providers = {};
let currentJobId = null;

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
  const runBtn = document.getElementById("run-btn");
  const stopBtn = document.getElementById("stop-btn");
  log.textContent = "";
  status.textContent = "Submitting...";
  document.getElementById("downloads").innerHTML = "";
  document.getElementById("stats").textContent = "";
  mv.removeAttribute("src");

  runBtn.disabled = true;
  stopBtn.disabled = true;
  stopBtn.textContent = "■ Stop";

  const r = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const t = await r.text();
    status.textContent = "Error: " + t;
    runBtn.disabled = false;
    return;
  }
  const { job_id } = await r.json();
  stopBtn.disabled = false;
  streamEvents(job_id);
});

document.getElementById("stop-btn").addEventListener("click", async () => {
  const stopBtn = document.getElementById("stop-btn");
  const status = document.getElementById("status");
  if (!currentJobId) return;
  stopBtn.disabled = true;
  stopBtn.textContent = "Cancelling...";
  status.textContent = "Cancelling — waiting for subprocess to exit...";
  try {
    await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
  } catch (e) {
    status.textContent = "Cancel request failed: " + e;
    stopBtn.disabled = false;
    stopBtn.textContent = "■ Stop";
  }
  // SSE will deliver the synthetic 'done' next; the onmessage handler
  // re-enables the run button and shows partial downloads.
});

function streamEvents(jobId) {
  currentJobId = jobId;
  const log = document.getElementById("log");
  const status = document.getElementById("status");
  const mv = document.getElementById("mv");
  const runBtn = document.getElementById("run-btn");
  const stopBtn = document.getElementById("stop-btn");
  const es = new EventSource(`/api/jobs/${jobId}/events`);

  function finishStream() {
    runBtn.disabled = false;
    stopBtn.disabled = true;
    stopBtn.textContent = "■ Stop";
    currentJobId = null;
  }

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
      // <model-viewer> doesn't reliably reload when only the query string
      // changes (server appends ?t=<ts> for cache-busting, but the element
      // may keep the old scene around and the new model renders on top of it,
      // looking like "another model" instead of replacing it). Force a clean
      // reload by clearing src, flushing layout, then setting the new URL.
      mv.removeAttribute("src");
      void mv.offsetWidth; // force reflow so model-viewer tears down the scene
      mv.setAttribute("src", msg.glb);
      status.textContent = "Live: intermediate model updated";
    }
    if (msg.done) {
      const tokenStr = msg.tokens != null ? ` · tokens: ${msg.tokens}` : "";
      const apiStr = msg.api_calls != null ? ` · API calls: ${msg.api_calls}` : "";
      if (msg.cancelled) {
        status.textContent = `Cancelled — partial artifacts available for download${tokenStr}${apiStr}`;
      } else {
        status.textContent = `Done — error_type: ${msg.error_type}${tokenStr}${apiStr}`;
      }
      es.close();
      finishStream();
      showResult(jobId, msg);
    }
    if (msg.error) {
      status.textContent = "Error: " + msg.error;
      log.textContent += "✗ " + msg.error + "\n";
      es.close();
      finishStream();
    }
  };

  es.onerror = () => {
    status.textContent = "Connection lost.";
    es.close();
    finishStream();
  };
}

function showResult(jobId, msg) {
  const mv = document.getElementById("mv");
  const dl = document.getElementById("downloads");
  dl.innerHTML = "";

  if (msg.glb) {
    mv.setAttribute("src", `/api/jobs/${jobId}/files/model.glb`);
  } else {
    mv.setAttribute("alt", "No GLB available — try downloading the STEP/STL");
  }

  // Only show download buttons for artifacts that actually exist on disk.
  const files = [
    ["GLB (preview)", "model.glb", msg.glb],
    ["STEP", "model.step", msg.step],
    ["STL", "model.stl", msg.stl],
    ["Python source", "source.py", msg.py],
    ["Measurements", "measurements.json", msg.measurements],
    ["Runtime diagnostics", "missed.json", msg.missed],
  ];
  for (const [label, fname, path] of files) {
    if (!path) continue;
    const a = document.createElement("a");
    a.href = `/api/jobs/${jobId}/files/${fname}`;
    a.textContent = `⬇ ${label}`;
    a.className = "dl-btn";
    dl.appendChild(a);
  }

  const statsStr = [];
  if (msg.tokens != null) statsStr.push(`Tokens: ${msg.tokens}`);
  if (msg.api_calls != null) statsStr.push(`API calls: ${msg.api_calls}`);
  document.getElementById("stats").textContent = statsStr.join(" · ");
}

loadSchema();
