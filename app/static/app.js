/* 病例切分平台前端（原生 JS，无外部依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let currentVersion = null;
let currentState = null;
let lastPreview = null; // {token, version, result, input}
let lastReview = null;  // {manifestId, result, fileName}

// --------------------------------------------------------------------------- //
// 基础请求
// --------------------------------------------------------------------------- //
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch { /* 非 JSON */ }
  if (!resp.ok) {
    const msg = data?.detail || `请求失败（HTTP ${resp.status}）`;
    const errs = data?.errors ? `：${data.errors.join("；")}` : "";
    throw new Error(msg + errs);
  }
  return data;
}

function setMsg(id, text, ok = false) {
  const el = $(id);
  el.textContent = text || "";
  el.className = "msg " + (ok ? "ok" : "err");
}

function downloadCsv(filename, content) {
  const blob = new Blob(["﻿" + content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 标签页
// --------------------------------------------------------------------------- //
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// --------------------------------------------------------------------------- //
// 模板下载
// --------------------------------------------------------------------------- //
$("tplCases").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("cases_template.csv", "病例号,患者号,标签\nC001,P001,1\nC002,P001,0\nC003,P002,1\n");
});
$("tplRel").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("relatives_template.csv", "患者A,患者B\nP001,P002\n");
});
$("tplCatalog").addEventListener("click", (e) => {
  e.preventDefault();
  const h = "a".repeat(64);
  downloadCsv(
    "catalog_template.csv",
    `病例号,影像号,SHA-256\nC001,IMG0001,${h}\nC001,IMG0002,${h}\nC002,IMG0003,${h}\n`
  );
});

// --------------------------------------------------------------------------- //
// 上传
// --------------------------------------------------------------------------- //
async function uploadCases() {
  const f = $("casesFile").files[0];
  if (!f) return setMsg("casesMsg", "请先选择病例 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("casesMsg", "上传中…");
  try {
    const r = await api("/api/upload/cases", { method: "POST", body: fd });
    setMsg("casesMsg", `已上传 ${r.case_count} 个病例，新版本 v${r.dataset_version}。${r.notice}`, true);
    $("casesFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("casesMsg", err.message); }
}

async function uploadRelatives() {
  const f = $("relFile").files[0];
  if (!f) return setMsg("relMsg", "请先选择亲属关系 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("relMsg", "上传中…");
  try {
    const r = await api("/api/upload/relatives", { method: "POST", body: fd });
    setMsg("relMsg", `已上传 ${r.pair_count} 对亲属关系，新版本 v${r.dataset_version}`, true);
    $("relFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("relMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 渲染当前数据
// --------------------------------------------------------------------------- //
function renderState() {
  const version = currentState.dataset_version;
  const input = currentState.input;
  currentVersion = version;
  $("currentVersion").textContent = "v" + version;

  const totals = currentState.label_totals;
  $("totL0").textContent = totals[0];
  $("totL1").textContent = totals[1];
  $("t_l0").textContent = totals[0];
  $("t_l1").textContent = totals[1];

  $("overview").innerHTML = [
    ["病例数", input.cases.length],
    ["患者数", currentState.patient_count],
    ["亲属对数", input.relatives.length],
    ["锁定数", Object.keys(input.locks).length],
  ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");

  $("caseCount").textContent = input.cases.length;
  $("caseTable").querySelector("tbody").innerHTML = input.cases
    .map((c) => `<tr><td>${escapeHtml(c.case_id)}</td><td>${escapeHtml(c.patient_id)}</td><td>${c.label}</td></tr>`)
    .join("");

  $("relCount").textContent = input.relatives.length;
  $("relTable").querySelector("tbody").innerHTML = input.relatives
    .map((r) => `<tr><td>${escapeHtml(r.patient_a)}</td><td>${escapeHtml(r.patient_b)}</td></tr>`)
    .join("");

  // 配额回填（仅当输入框未被聚焦时，避免打断输入）
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const el = $("q_" + key);
    if (document.activeElement !== el) el.value = input.quotas[key] ?? 0;
  }
  renderLocks();
  renderManifestTable();
}

// --------------------------------------------------------------------------- //
// 配额
// --------------------------------------------------------------------------- //
async function saveQuotas() {
  const payload = {};
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const v = Number($("q_" + key).value);
    if (!Number.isInteger(v) || v < 0) {
      return setMsg("quotaMsg", `配额 ${key} 必须是非负整数`);
    }
    payload[key] = v;
  }
  try {
    const r = await api("/api/quotas", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setMsg("quotaMsg", `配额已保存，新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) { setMsg("quotaMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 锁定表
// --------------------------------------------------------------------------- //
function renderLocks() {
  if (!currentState) return;
  const input = currentState.input;
  // 轮询重建前保留用户尚未保存的选择，避免被 4 秒刷新冲掉
  const prior = {};
  document.querySelectorAll("[data-lock-case]").forEach((sel) => {
    prior[sel.dataset.lockCase] = sel.value;
  });
  const filter = $("lockFilter").value.trim().toLowerCase();
  const rows = input.cases
    .filter((c) => !filter || c.case_id.toLowerCase().includes(filter) ||
      c.patient_id.toLowerCase().includes(filter))
    .map((c) => {
      const saved = input.locks[c.case_id];
      const effective = Object.prototype.hasOwnProperty.call(prior, c.case_id)
        ? prior[c.case_id]
        : (saved === undefined ? "" : String(saved));
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td><select data-lock-case="${escapeHtml(c.case_id)}">
          <option value=""${effective === "" ? " selected" : ""}>不锁定</option>
          <option value="1"${effective === "1" ? " selected" : ""}>训练集（组1）</option>
          <option value="2"${effective === "2" ? " selected" : ""}>评估集（组2）</option>
        </select></td></tr>`;
    }).join("");
  $("lockTable").querySelector("tbody").innerHTML = rows;
}

async function saveLocks() {
  const locks = [];
  document.querySelectorAll("[data-lock-case]").forEach((sel) => {
    if (sel.value !== "") locks.push({ case_id: sel.dataset.lockCase, group: Number(sel.value) });
  });
  try {
    const r = await api("/api/locks", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locks }),
    });
    setMsg("lockMsg", `锁定已保存（${r.lock_count} 项），新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) { setMsg("lockMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 求解
// --------------------------------------------------------------------------- //
async function runSolve() {
  $("solveStatus").textContent = "z3 求解中…";
  $("solveStatus").className = "msg";
  $("satBox").classList.add("hidden");
  $("unsatBox").classList.add("hidden");
  try {
    const r = await api("/api/solve", { method: "POST" });
    lastPreview = {
      token: r.preview_token,
      version: r.dataset_version,
      result: r.result,
      input: r.input_snapshot,
    };
    renderSolveResult();
    $("solveStatus").textContent =
      r.result.status === "sat" ? `求解成功（依据 v${r.dataset_version}）` : "约束无解";
    $("solveStatus").className = "msg " + (r.result.status === "sat" ? "ok" : "err");
  } catch (err) {
    $("solveStatus").textContent = err.message;
    $("solveStatus").className = "msg err";
  }
}

function previewIsStale() {
  return !lastPreview || lastPreview.version !== currentVersion;
}

function renderSolveResult() {
  if (!lastPreview) return;
  const { result } = lastPreview;
  if (result.status === "sat") {
    $("satBox").classList.remove("hidden");
    $("unsatBox").classList.add("hidden");
    const counts = result.stats.counts;
    $("solveStats").innerHTML = [
      ["连通分量数", result.stats.component_count],
      ["约束总数", result.constraint_count],
      ["训练集 标签0/1", `${counts.g1_label0} / ${counts.g1_label1}`],
      ["评估集 标签0/1", `${counts.g2_label0} / ${counts.g2_label1}`],
    ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");
    const stale = previewIsStale();
    $("previewWarn").classList.toggle("hidden", !stale);
    $("publishBtn").disabled = stale;
    renderAssignments();
  } else {
    $("satBox").classList.add("hidden");
    $("unsatBox").classList.remove("hidden");
    $("unsatSummary").textContent = result.summary;
    $("conflictTable").querySelector("tbody").innerHTML = result.conflicts
      .map((c, i) => `<tr>
        <td>${i + 1}</td>
        <td><span class="pill ${c.kind === "quota" ? "dead" : "warn"}">${escapeHtml(c.kind_name)}</span></td>
        <td>${escapeHtml(c.description)}</td>
        <td><code>${escapeHtml(JSON.stringify(c.detail))}</code></td>
      </tr>`).join("");
  }
}

function renderAssignments() {
  if (!lastPreview || lastPreview.result.status !== "sat") return;
  const grpFilter = $("filterGroup").value;
  const labFilter = $("filterLabel").value;
  const locks = lastPreview.input.locks || {};
  const assigns = lastPreview.result.assignments;
  const rows = lastPreview.input.cases
    .filter((c) => (!grpFilter || String(assigns[c.case_id]) === grpFilter) &&
      (!labFilter || String(c.label) === labFilter))
    .map((c) => {
      const g = assigns[c.case_id];
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td>${g}</td>
        <td>${g === 1 ? "训练集" : "评估集"}</td>
        <td>${c.case_id in locks ? "是 → " + (locks[c.case_id] === 1 ? "训练集" : "评估集") : "否"}</td>
      </tr>`;
    }).join("");
  $("assignTable").querySelector("tbody").innerHTML = rows;
}

// --------------------------------------------------------------------------- //
// 发布
// --------------------------------------------------------------------------- //
async function publishManifest() {
  if (!lastPreview || !lastPreview.token) return;
  setMsg("publishMsg", "发布中…");
  try {
    await api("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_token: lastPreview.token }),
    });
    setMsg("publishMsg", "清单已发布并与输入依据一起持久化", true);
    await refreshAll();
  } catch (err) {
    setMsg("publishMsg", err.message);
    if (String(err.message).includes("失效")) {
      $("publishBtn").disabled = true;
      $("previewWarn").classList.remove("hidden");
    }
  }
}

// --------------------------------------------------------------------------- //
// 已发布清单
// --------------------------------------------------------------------------- //
function renderManifestTable() {
  if (!currentState) return;
  const tbody = $("manifestTable").querySelector("tbody");
  if (!currentState.manifests.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="hint">暂无已发布清单</td></tr>`;
    return;
  }
  tbody.innerHTML = currentState.manifests.map((m) => {
    const status = m.expired
      ? `<span class="pill dead">已永久失效${m.invalidated ? "（输入已变动）" : "（版本不匹配）"}</span>`
      : `<span class="pill ok">有效（v${m.dataset_version}）</span>`;
    return `<tr>
      <td><code>${escapeHtml(m.manifest_id.slice(0, 8))}…</code></td>
      <td>v${m.dataset_version}</td>
      <td>${escapeHtml(m.created_at)}</td>
      <td>${m.case_count ?? ""}</td>
      <td>${status}</td>
      <td>
        <button onclick="showManifest('${m.manifest_id}')">查看依据</button>
        <button ${m.expired ? "disabled" : ""} onclick="downloadManifest('${m.manifest_id}')">下载 CSV</button>
      </td></tr>`;
  }).join("");
}

async function showManifest(id) {
  try {
    const m = await api(`/api/manifests/${id}`);
    $("manifestDetail").classList.remove("hidden");
    $("manifestJson").textContent = JSON.stringify(
      { manifest_id: m.manifest_id, dataset_version: m.dataset_version,
        created_at: m.created_at, expired: m.expired,
        input_basis: m.input, split_result: m.result }, null, 2);
  } catch (err) { alert(err.message); }
}

async function downloadManifest(id) {
  const resp = await fetch(`/api/manifests/${id}/download`);
  if (!resp.ok) {
    let msg = `下载被拒绝（HTTP ${resp.status}）`;
    try { msg = (await resp.json()).detail || msg; } catch { /* ignore */ }
    alert(msg);
    await refreshAll();
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `split_manifest_${id.slice(0, 8)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 影像交付审查
// --------------------------------------------------------------------------- //
async function loadReviewManifests() {
  const sel = $("reviewManifest");
  try {
    const r = await api("/api/review/manifests");
    if (!r.manifests.length) {
      sel.innerHTML = `<option value="">（暂无有效清单，请先在“求解与发布”发布清单）</option>`;
      $("reviewManifestHint").textContent = "当前没有任何有效清单。";
      return;
    }
    sel.innerHTML = r.manifests.map((m) =>
      `<option value="${escapeHtml(m.manifest_id)}">` +
      `${escapeHtml(m.manifest_id.slice(0, 8))}… · v${m.dataset_version} · ${m.case_count ?? ""} 病例 · ${escapeHtml(m.created_at)}</option>`
    ).join("");
    $("reviewManifestHint").textContent =
      `共 ${r.manifests.length} 份有效清单（当前输入版本 v${r.current_version}）。`;
  } catch (err) {
    sel.innerHTML = `<option value="">（加载失败）</option>`;
    $("reviewManifestHint").textContent = err.message;
  }
}

function shortDigest(d) {
  return d ? d.slice(0, 10) + "…" + d.slice(-6) : "";
}

async function runReview() {
  const manifestId = $("reviewManifest").value;
  const f = $("catalogFile").files[0];
  setMsg("reviewMsg", "");
  $("reviewInvalidBanner").classList.add("hidden");
  $("reviewResult").classList.add("hidden");
  $("reviewDownloadBtn").disabled = true;
  if (!manifestId) return setMsg("reviewMsg", "请先选择一份有效清单");
  if (!f) return setMsg("reviewMsg", "请先选择影像目录 CSV 文件");

  const fd = new FormData();
  fd.append("file", f);
  setMsg("reviewMsg", "审查中…");
  try {
    const r = await api(`/api/review/${encodeURIComponent(manifestId)}`, { method: "POST", body: fd });
    lastReview = { manifestId, result: r.result, fileName: f.name };
    renderReviewResult();
    setMsg("reviewMsg", `审查完成（依据 v${r.dataset_version}）：可交付 ${r.result.stats.deliverable_count} 例，隔离 ${r.result.stats.isolated_count} 例`, true);
  } catch (err) {
    lastReview = null;
    setMsg("reviewMsg", err.message);
    if (String(err.message).includes("失效")) {
      $("reviewInvalidBanner").classList.remove("hidden");
      loadReviewManifests();
    }
  }
}

function renderReviewResult() {
  if (!lastReview) return;
  const { result } = lastReview;
  const s = result.stats;
  $("reviewResult").classList.remove("hidden");
  $("reviewDownloadBtn").disabled = false;

  $("reviewStats").innerHTML = [
    ["清单病例总数", s.case_total],
    ["可交付病例", s.deliverable_count],
    ["隔离病例", s.isolated_count],
    ["缺片病例", s.missing_count],
    ["跨组重复摘要", s.duplicate_digest_count],
    ["分量冲突数", s.conflict_edge_count],
    ["隔离连通分量", s.isolated_component_count],
    ["连通分量总数", s.component_total],
  ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");

  $("reviewSummary").textContent =
    `已按“隔离病例总数最少”求解并独立复核：保留病例不存在任何跨组重复摘要；` +
    `缺片 ${s.missing_count} 例、因重复摘要/亲属连带隔离的分量 ${s.isolated_component_count} 个。` +
    `同一份清单与目录重复审查将得到完全一致的建议。`;

  const edgeBody = $("conflictEdgeTable").querySelector("tbody");
  if (!result.conflict_edges.length) {
    edgeBody.innerHTML = `<tr><td colspan="5" class="hint">无跨组重复摘要冲突</td></tr>`;
  } else {
    edgeBody.innerHTML = result.conflict_edges.map((e) => `<tr>
      <td>${escapeHtml(e.edge_id)}</td>
      <td><code title="${escapeHtml(e.digest)}">${escapeHtml(shortDigest(e.digest))}</code></td>
      <td>${escapeHtml(e.component_a)}（${escapeHtml(e.group_a_name)}）</td>
      <td>${escapeHtml(e.component_b)}（${escapeHtml(e.group_b_name)}）</td>
      <td>${escapeHtml(e.case_a)} ⇄ ${escapeHtml(e.case_b)}</td>
    </tr>`).join("");
  }
  renderReviewCases();
}

function renderReviewCases() {
  if (!lastReview) return;
  const dFilter = $("reviewDecisionFilter").value;
  const gFilter = $("reviewGroupFilter").value;
  const rows = lastReview.result.cases
    .filter((c) => (!dFilter || c.decision === dFilter) && (!gFilter || String(c.group) === gFilter))
    .map((c) => {
      const reasons = c.reasons.length
        ? c.reasons.map((r) => `<span class="pill ${r.kind === "missing" ? "dead" : "warn"}">${escapeHtml(r.kind_name)}</span> ${escapeHtml(r.detail)}`).join("<br>")
        : '<span class="pill ok">无异常</span>';
      const pill = c.decision === "deliverable"
        ? '<span class="pill ok">可交付</span>'
        : '<span class="pill dead">隔离</span>';
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${escapeHtml(c.group_name)}</td>
        <td>${escapeHtml(c.component_id)}</td>
        <td>${c.image_count}</td>
        <td>${pill}</td>
        <td style="white-space:normal;min-width:280px">${reasons}</td>
      </tr>`;
    }).join("");
  $("reviewCaseTable").querySelector("tbody").innerHTML = rows
    || `<tr><td colspan="7" class="hint">没有匹配的病例</td></tr>`;
}

async function downloadReviewCsv() {
  if (!lastReview) return;
  const f = $("catalogFile").files[0];
  if (!f) return setMsg("reviewMsg", "请重新选择影像目录文件后再下载");
  const fd = new FormData();
  fd.append("file", f);
  const resp = await fetch(`/api/review/${encodeURIComponent(lastReview.manifestId)}/download`, {
    method: "POST",
    body: fd,
  });
  if (!resp.ok) {
    let msg = `下载被拒绝（HTTP ${resp.status}）`;
    try { msg = (await resp.json()).detail || msg; } catch { /* ignore */ }
    setMsg("reviewMsg", msg);
    if (String(msg).includes("失效")) {
      $("reviewInvalidBanner").classList.remove("hidden");
      loadReviewManifests();
    }
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `delivery_review_${lastReview.manifestId.slice(0, 8)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 版本日志
// --------------------------------------------------------------------------- //
async function loadVersionLog() {
  try {
    const r = await api("/api/version-log");
    $("logTable").querySelector("tbody").innerHTML = r.log.map((e) => `<tr>
      <td>v${e.dataset_version}</td>
      <td>${escapeHtml(e.change_type)}</td>
      <td><code>${escapeHtml(JSON.stringify(e.detail))}</code></td>
      <td>${escapeHtml(e.created_at)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="hint">暂无变动记录</td></tr>`;
  } catch (err) { /* 轮询场景静默 */ }
}

// --------------------------------------------------------------------------- //
// 全局刷新与后台轮询（支持两个窗口交错操作）
// --------------------------------------------------------------------------- //
async function refreshAll(showStaleBanner = false) {
  try {
    const prevVersion = currentVersion;
    currentState = await api("/api/state");
    renderState();
    loadReviewManifests();
    $("healthBadge").textContent = "已连接";
    $("healthBadge").className = "badge";
    const changed = prevVersion !== null && prevVersion !== currentState.dataset_version;
    if (changed || showStaleBanner) {
      $("staleBanner").classList.toggle("hidden", !changed && !showStaleBanner);
      if (lastPreview && lastPreview.version !== currentVersion) {
        $("previewWarn").classList.remove("hidden");
        $("publishBtn").disabled = true;
      }
    }
    if (lastPreview) renderSolveResult();
  } catch (err) {
    $("healthBadge").textContent = "连接中断";
    $("healthBadge").className = "badge down";
  }
}

setInterval(refreshAll, 4000);
setInterval(() => {
  if ($("tab-log").classList.contains("active")) loadVersionLog();
}, 5000);

refreshAll();
