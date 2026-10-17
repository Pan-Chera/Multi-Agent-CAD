// Exercise the actual frontend script with a small DOM and fetch substitute.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const data = JSON.parse(fs.readFileSync(0, "utf8"));
const staticDir = path.resolve(__dirname, "../../multi_agent_cad/web/static");
const source = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
assert.match(html, /option value="openrouter"/);
assert.match(html, /option value="custom"/);

async function page(baseUrl) {
  const elements = new Map();
  class Element {
    constructor() { this.value = ""; this.dataset = {}; this.listeners = {}; this.children = []; }
    set innerHTML(value) {
      this.children = [];
      for (const match of value.matchAll(/id="([^"]+)"[^>]*value="([^"]*)"/g)) {
        const input = new Element();
        input.value = match[2];
        elements.set(match[1], input);
      }
    }
    addEventListener(event, listener) { this.listeners[event] = listener; }
    appendChild(child) { this.children.push(child); }
    setAttribute() {}
    removeAttribute() {}
  }
  for (const match of html.matchAll(/id="([^"]+)"/g)) elements.set(match[1], new Element());
  const tbody = new Element();
  const get = id => { assert.ok(elements.has(id), `Missing element: ${id}`); return elements.get(id); };
  const submitted = [];
  const context = vm.createContext({
    URL, encodeURIComponent,
    document: {getElementById: get, createElement: () => new Element(),
      querySelector: selector => { assert.equal(selector, "#stage-table tbody"); return tbody; }},
    fetch: async (url, options) => {
      if (url === "/api/config/schema") return {json: async () => ({...data,
        config: {...data.config, DS_BASE_URL: baseUrl}})};
      assert.equal(url, "/api/run");
      assert.equal(options.headers["X-MAC-CSRF"], "1");
      submitted.push(JSON.parse(options.body));
      return {ok: false, text: async () => "probe stops before model execution"};
    },
  });
  await vm.runInContext(source, context);
  const fire = (id, event) => get(id).listeners[event]({target: get(id)});
  return {get, fire, context, submitted};
}

(async () => {
  for (const [url, expected] of [
    [data.providers.qwen.ds_base_url, "qwen"],
    ["https://openrouter.ai/api/v1/", "openrouter"],
    [data.providers.openai.ds_base_url, "openai"],
    ["https://openrouter.ai.evil.example/api/v1", "custom"],
    ["https://openrouter.ai/api/v10", "custom"],
    ["https://private.example/v1", "custom"],
  ]) {
    const p = await page(url);
    assert.equal(p.get("provider").value, expected);
    assert.equal(p.get("DS_BASE_URL").value, url);
    assert.equal(p.get("ARCHITECT_MODEL").value, data.config.ARCHITECT_MODEL);
    assert.equal(p.get("AIDER_MODEL").value, data.config.AIDER_MODEL);
  }
  const p = await page(data.config.DS_BASE_URL);
  p.get("provider").value = "openrouter";
  p.fire("provider", "change");
  assert.equal(p.get("DS_BASE_URL").value, data.providers.openrouter.ds_base_url);
  assert.equal(p.get("SPEC_PLANNER_MODEL").value, data.providers.openrouter.model_hint);
  assert.equal(p.get("AIDER_MODEL").value, data.providers.openrouter.aider_model_hint);
  p.get("ARCHITECT_MODEL").value = "my/model";
  p.fire("ARCHITECT_MODEL", "input");
  p.get("AIDER_MODEL").value = "openai/my/repair-model";
  p.fire("AIDER_MODEL", "input");
  p.get("provider").value = "qwen";
  p.fire("provider", "change");
  assert.equal(p.get("ARCHITECT_MODEL").value, "my/model");
  assert.equal(p.get("AIDER_MODEL").value, "openai/my/repair-model");
  p.get("provider").value = "openrouter";
  p.fire("provider", "change");
  await p.fire("run-btn", "click");
  assert.equal(p.submitted[0].config.DS_BASE_URL, data.providers.openrouter.ds_base_url);
  assert.equal(p.submitted[0].config.AIDER_MODEL, "openai/my/repair-model");
  vm.runInContext("showResult('probe', {step:'part.step', stl:'part.stl', py:'source.py'});", p.context);
  assert.deepEqual(p.get("downloads").children.map(a => a.href), [
    "/api/jobs/probe/files/model.step", "/api/jobs/probe/files/model.stl",
    "/api/jobs/probe/files/source.py",
  ]);
  console.log("Provider detection, selection, custom models, submission and download links passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
