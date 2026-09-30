/* 页面逻辑与数据、校验分开维护：只负责调用 API 和渲染句级签署状态。 */
const $ = (id) => document.getElementById(id);
const auth = () => ({
  "Content-Type": "application/json",
  "X-User": $("user").value.trim(),
  "X-Role": $("role").value,
});

let currentVersion = null;
let currentReadiness = null;

async function call(url, opts = {}) {
  const r = await fetch(url, opts);
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(j.error || `HTTP ${r.status}`), { status: r.status });
  return j;
}

function banner(message, kind) {
  const el = $("banner");
  if (!message) { el.className = ""; el.textContent = ""; return; }
  el.className = `show ${kind}`;
  el.textContent = message;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function shortTime(iso) { return iso ? iso.replace("T", " ").replace("+00:00", "Z") : ""; }

function renderCues(approvals) {
  const tbody = $("cueRows");
  if (!approvals.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">该版本还没有字幕。</td></tr>';
    return;
  }
  tbody.innerHTML = approvals.map((a) => {
    let statusCell;
    if (a.has_valid_signature) {
      const who = a.valid_signatures.map((s) =>
        `<span class="sig-valid">✔ ${esc(s.reviewer)}</span> <small>${shortTime(s.signed_at)}</small>`).join("<br>");
      statusCell = `<span class="pill ok">有效签署</span><br>${who}`;
    } else if (a.invalidated.length) {
      const reasons = a.invalidated.map((s) =>
        `<div class="sig-invalid">✘ ${esc(s.reviewer)}：${esc(s.reason)}
         <small>（签署于 ${shortTime(s.signed_at)}，失效于 ${shortTime(s.invalidated_at)}）</small></div>`).join("");
      statusCell = `<span class="pill bad">已失效</span>${reasons}`;
    } else {
      statusCell = '<span class="pill maybe">待签署</span>';
    }
    return `<tr>
      <td>${a.cue_index}</td>
      <td><small>${a.start_ms} → ${a.end_ms}</small></td>
      <td>${esc(a.text)}<br><small class="muted">摘要 ${a.current_digest.slice(0, 12)}…</small></td>
      <td>${statusCell}</td>
      <td>${a.has_valid_signature ? "" : '<span class="muted">批量确认</span>'}</td>
    </tr>`;
  }).join("");
}

function renderReadiness(readiness, versions) {
  currentReadiness = readiness;
  currentVersion = versions.find((v) => v.id === Number($("version").value)) || null;
  $("versionStatus").textContent = currentVersion
    ? `当前状态：${currentVersion.status}，revision=${currentVersion.revision}`
    : "版本不存在";
  if (readiness.total_cues === 0) {
    $("readiness").textContent = "空版本：先保存字幕再提交复核。";
  } else if (readiness.ready) {
    $("readiness").innerHTML = `共 ${readiness.total_cues} 句，<span class="sig-valid">每一句都有对应当前内容的有效签署</span>，可以整版批准/交付。`;
  } else {
    $("readiness").innerHTML = `共 ${readiness.total_cues} 句，` +
      `<span class="sig-invalid">句号 ${readiness.missing_indexes.join("、")} 缺少有效签署</span>，整版批准与交付都会被拦下。`;
  }
}

async function refresh() {
  banner("");
  const vid = $("version").value;
  try {
    const [versions, approvals, readiness, comments, deliveries] = await Promise.all([
      call("/api/versions"),
      call(`/api/versions/${vid}/approvals`),
      call(`/api/versions/${vid}/readiness`),
      call(`/api/versions/${vid}/comments`).catch(() => ({ comments: [] })),
      call("/api/deliveries"),
    ]);
    renderCues(approvals.approvals);
    renderReadiness(readiness, versions.versions);
    const data = { versions, comments, deliveries };
    let node = $("lastData");
    if (!node) {
      node = document.createElement("pre"); node.id = "lastData";
      $("readiness").insertAdjacentElement("afterend", node);
    }
    node.textContent = JSON.stringify(data, null, 2);
    node.style.maxHeight = "220px";
  } catch (e) {
    $("cueRows").innerHTML = `<tr><td colspan="5" class="sig-invalid">${esc(e.message)}</td></tr>`;
    $("readiness").textContent = "";
  }
}

async function saveCue() {
  const node = $("cueResult"); node.hidden = false;
  try {
    const out = await call(`/api/versions/${$("version").value}/cues`, {
      method: "POST", headers: auth(), body: $("cue").value,
    });
    node.textContent = JSON.stringify(out, null, 2);
    banner(out.invalidated_approvals
      ? `已保存：该句 ${out.invalidated_approvals} 份旧签署失效（${out.invalidation_reason}），其余句子不受影响。`
      : "已保存。", "msg");
    refresh();
  } catch (e) { node.textContent = e.message; banner(e.message, "err"); }
}

async function addComment() {
  const node = $("commentResult"); node.hidden = false;
  try {
    const out = await call(`/api/versions/${$("version").value}/comments`, {
      method: "POST", headers: auth(),
      body: JSON.stringify({ time_ms: Number($("time").value), body: $("comment").value }),
    });
    node.textContent = JSON.stringify(out, null, 2);
    refresh();
  } catch (e) { node.textContent = e.message; }
}

function storedBatchKey() { return `subtitle_batch_${$("version").value}`; }

async function batchApprove() {
  // 批次号在本地留存：异常中断后原样重提，服务端按批次号去重，不生成两份签署。
  let batchId = $("batchId").value.trim() || localStorage.getItem(storedBatchKey());
  try {
    const out = await call(`/api/versions/${$("version").value}/cues/approve`, {
      method: "POST", headers: auth(),
      body: JSON.stringify(batchId ? { batch_id: batchId } : {}),
    });
    $("batchId").value = out.batch_id;
    localStorage.setItem(storedBatchKey(), out.batch_id);
    const parts = [`新签署 ${out.signed.length} 句：[${out.signed.join("、") || "无"}]`];
    if (out.already_signed.length) parts.push(`此前已签 ${out.already_signed.length} 句（幂等跳过）`);
    if (out.missing_indexes.length) parts.push(`句号不存在：${out.missing_indexes.join("、")}`);
    banner(parts.join("；") + `。批次号 ${out.batch_id}`, "msg");
    refresh();
  } catch (e) { banner(e.message, "err"); }
}

async function action(name) {
  const node = $("stateResult"); node.hidden = false;
  try {
    const out = await call(`/api/versions/${$("version").value}/${name}`, {
      method: "POST", headers: auth(), body: "{}",
    });
    node.textContent = JSON.stringify(out, null, 2);
    banner("");
    refresh();
  } catch (e) {
    node.textContent = e.message;
    banner(e.message, "err");
  }
}

async function review(decision) {
  const node = $("stateResult"); node.hidden = false;
  try {
    const out = await call(`/api/versions/${$("version").value}/review`, {
      method: "POST", headers: auth(), body: JSON.stringify({ decision, comment: "" }),
    });
    node.textContent = JSON.stringify(out, null, 2);
    banner(decision === "approve" ? "整版批准成功。" : "已退回修改：未受改动影响的句子保留有效签署。", "msg");
    refresh();
  } catch (e) {
    node.textContent = e.message;
    banner(e.message, "err");
  }
}

$("version").addEventListener("change", () => {
  $("batchId").value = localStorage.getItem(storedBatchKey()) || "";
  refresh();
});
$("batchId").addEventListener("input", () => localStorage.setItem(storedBatchKey(), $("batchId").value.trim()));
refresh();
