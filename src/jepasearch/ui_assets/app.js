"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const $ = (selector) => document.querySelector(selector);
const state = { files: [], job: null, results: [], resultProvenance: null, engineTouched: false, queryMode: "segment", candidateMode: "segment", activePreview: "query", selectedCandidate: null, pendingDecisions: new Set(), searchSubmitting: false, indexSubmitting: false, cancelSubmitting: false, searchSequence: 0, stateSequence: 0, pollSequence: 0, referenceSequence: 0, toastTimer: 0, pollTimer: 0 };

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("visible");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => node.classList.remove("visible"), 3600);
}

async function api(path, { method = "GET", body = undefined } = {}) {
  const headers = {};
  if (method !== "GET") {
    headers["X-CSRF-Token"] = csrf;
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", cache: "no-store" });
  const contentType = response.headers.get("Content-Type") || "";
  const value = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(value?.error || `ローカルリクエストに失敗しました (${response.status})`);
  return value;
}

function fmtTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function previewRange(start, end, player) {
  const begin = Math.max(0, Number(start) || 0);
  const rawEnd = Math.max(begin + 0.1, Number(end) || begin + 0.1);
  const duration = Number(player?.duration);
  return [begin, Math.min(Number.isFinite(duration) ? duration : rawEnd, rawEnd)];
}

function byteLabel(size) {
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / (1024 * 1024 * 1024) >= 1 ? size / (1024 * 1024 * 1024) : size / (1024 * 1024)).toFixed(1)} ${size / (1024 * 1024 * 1024) >= 1 ? "GB" : "MB"}`;
}

function renderStoreReadiness(data) {
  $("#store-label").textContent = data.store_label || "ローカル保存先";
  const ready = Boolean(data.store_ready);
  $("#export-references").setAttribute("aria-disabled", String(!ready));
  $("#export-references").classList.toggle("button-disabled", !ready);
}

function empty(node, text) {
  node.replaceChildren();
  node.classList.add("empty-state");
  node.textContent = text;
}

function engineName(engine) {
  return engine === "color-motion" ? "Color + Motion" : engine === "vjepa2" ? "V-JEPA 2" : engine || "不明";
}

function renderResultProvenance() {
  const provenance = state.resultProvenance;
  const label = $("#result-provenance");
  const mismatch = $("#result-engine-mismatch");
  $("#engine-summary").textContent = engineName($("#engine").value);
  if (!provenance) {
    label.textContent = "検索結果の方式とfingerprintは検索後に表示されます。";
    mismatch.hidden = true;
    mismatch.textContent = "";
    return;
  }
  label.textContent = provenance.fingerprint
    ? `生成エンジン: ${engineName(provenance.engine)} · fingerprint: ${provenance.fingerprint}`
    : `検索エンジン: ${engineName(provenance.engine)} · fingerprintを取得中`;
  const selected = $("#engine").value;
  const differs = Boolean(provenance.engine && selected && provenance.engine !== selected);
  mismatch.hidden = !differs;
  mismatch.textContent = differs
    ? `表示中の候補は${engineName(provenance.engine)}で生成されました。現在の選択は${engineName(selected)}です。新しい検索を実行するまで、候補は前の検索結果のままです。`
    : "";
}

function queryFileIndexedForSelectedEngine() {
  const file = state.files.find((entry) => entry.id === $("#query-file").value);
  return Boolean(file?.indexed_pipelines?.[$("#engine").value]);
}

function renderFiles() {
  const list = $("#library-list");
  const query = $("#query-file");
  const priorQueryId = query.value;
  list.replaceChildren();
  query.replaceChildren();
  if (!state.files.length) {
    empty(list, "動画を選ぶと、ここにローカル素材が表示されます。");
    query.add(new Option("先に動画を選択", ""));
    $("#library-count").textContent = "0本";
    $("#start-index").disabled = true;
    $("#video-empty").hidden = false;
    setPlayerSource($("#query-player"), "");
    return;
  }
  list.classList.remove("empty-state");
  query.add(new Option("見本に使う動画を選択", ""));
  for (const file of state.files) {
    const row = document.createElement("label");
    row.className = "file-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = file.id;
    checkbox.checked = true;
    checkbox.setAttribute("aria-label", `${file.name} を索引対象にする`);
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.name;
    const marker = document.createElement("span");
    marker.className = "file-state";
    marker.textContent = file.indexed ? "索引済み" : byteLabel(file.size);
    row.append(checkbox, name, marker);
    list.append(row);
    query.add(new Option(file.name, file.id));
  }
  query.value = state.files.some((file) => file.id === priorQueryId) ? priorQueryId : state.files[0].id;
  $("#library-count").textContent = `${state.files.length}本`;
  $("#start-index").disabled = false;
  $("#video-empty").hidden = Boolean(query.value);
}

function setPlayerSource(player, fileId) {
  const status = player.id === "query-player" ? $("#query-playback-status") : $("#candidate-playback-status");
  status.hidden = true;
  status.textContent = "";
  if (!fileId) {
    player.pause();
    player.removeAttribute("src");
    delete player.dataset.rangeStart;
    delete player.dataset.rangeEnd;
    player.load();
    return;
  }
  const src = `/media/${encodeURIComponent(fileId)}`;
  if (player.getAttribute("src") !== src) {
    player.pause();
    delete player.dataset.rangeStart;
    delete player.dataset.rangeEnd;
    player.src = src;
    player.load();
  }
}

for (const [player, status] of [
  [$("#query-player"), $("#query-playback-status")],
  [$("#candidate-player"), $("#candidate-playback-status")],
]) {
  player.addEventListener("loadedmetadata", () => { status.hidden = true; status.textContent = ""; });
  player.addEventListener("error", () => {
    const code = player.error?.code;
    status.textContent = code === MediaError.MEDIA_ERR_DECODE || code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED
      ? "この動画をブラウザで再生できません。索引できてもcodecが非対応の場合があります。ブラウザ対応形式（例: MP4/H.264）を確認してください。"
      : code === MediaError.MEDIA_ERR_NETWORK
        ? "ローカル動画を読み込めませんでした。動画を選び直して再試行してください。"
        : "動画の再生に失敗しました。動画形式とファイル状態を確認してください。";
    status.hidden = false;
  });
}

function updateActivePreview() {
  const query = state.activePreview === "query";
  $("#query-view").hidden = !query;
  $("#candidate-view").hidden = query || !state.selectedCandidate;
  $("#query-view-tab").classList.toggle("is-active", query);
  $("#candidate-view-tab").classList.toggle("is-active", !query && Boolean(state.selectedCandidate));
  $("#query-view-tab").setAttribute("aria-selected", String(query));
  $("#candidate-view-tab").setAttribute("aria-selected", String(!query && Boolean(state.selectedCandidate)));
  $("#query-view-tab").tabIndex = query ? 0 : -1;
  $("#candidate-view-tab").tabIndex = !query && state.selectedCandidate ? 0 : -1;
  $("#query-context").classList.toggle("is-hidden", !query);
  $("#candidate-context").classList.toggle("is-hidden", query || !state.selectedCandidate);
  if (query) {
    const start = Number($("#query-start").value);
    const [rangeStart, rangeEnd] = previewRange(
      state.queryMode === "segment" ? start : start - 5,
      state.queryMode === "segment" ? start + 2 : start + 7,
      $("#query-player"),
    );
    $("#active-range-label").textContent = `${fmtTime(rangeStart)} – ${fmtTime(rangeEnd)}`;
  } else if (state.selectedCandidate) {
    const result = state.selectedCandidate;
    const [start, end] = previewRange(
      state.candidateMode === "segment" ? result.start_s : result.start_s - 5,
      state.candidateMode === "segment" ? result.end_s : result.end_s + 5,
      $("#candidate-player"),
    );
    $("#active-range-label").textContent = `${fmtTime(start)} – ${fmtTime(end)}`;
  }
}

function showPreview(view) {
  state.activePreview = view === "candidate" && state.selectedCandidate ? "candidate" : "query";
  updateActivePreview();
}

function applyRange(player, start, end, shouldPlay = true) {
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;
  const begin = Math.max(0, start);
  const finish = Math.max(begin + 0.1, Math.min(Number.isFinite(player.duration) ? player.duration : end, end));
  player.dataset.rangeStart = String(begin);
  player.dataset.rangeEnd = String(finish);
  const seek = () => {
    try { player.currentTime = begin; } catch { /* Metadata may still be loading. */ }
    if (shouldPlay) player.play().catch(() => {});
  };
  if (player.readyState >= 1) seek();
  else player.addEventListener("loadedmetadata", seek, { once: true });
}

function playerRangeEnd(player) {
  const value = Number(player.dataset.rangeEnd);
  return Number.isFinite(value) ? value : Infinity;
}

for (const player of [$("#query-player"), $("#candidate-player")]) {
  player.addEventListener("play", () => {
    const start = Number(player.dataset.rangeStart);
    const end = playerRangeEnd(player);
    if (Number.isFinite(start) && Number.isFinite(end) && player.currentTime >= end - 0.05) {
      player.currentTime = start;
    }
  });
  player.addEventListener("timeupdate", () => {
    if (!player.paused && player.currentTime >= playerRangeEnd(player)) {
      player.pause();
      player.currentTime = playerRangeEnd(player);
    }
  });
}

$("#query-view-tab").addEventListener("click", () => showPreview("query"));
$("#candidate-view-tab").addEventListener("click", () => showPreview("candidate"));
$(".view-tabs").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  if (event.key === "ArrowLeft") {
    showPreview("query");
    $("#query-view-tab").focus();
  } else if (!$("#candidate-view-tab").disabled) {
    showPreview("candidate");
    $("#candidate-view-tab").focus();
  }
});

function updateQueryPreview() {
  const fileId = $("#query-file").value;
  const start = Number($("#query-start").value);
  const end = start + 2;
  $("#query-end").textContent = Number.isFinite(start) ? end.toFixed(3) : "—";
  $("#query-range-label").textContent = `${fmtTime(start)} – ${fmtTime(end)}`;
  const player = $("#query-player");
  const current = player.getAttribute("src") || "";
  const wanted = fileId ? `/media/${encodeURIComponent(fileId)}` : "";
  if (current !== wanted) setPlayerSource(player, fileId);
  $("#query-context").disabled = !fileId || !Number.isFinite(start) || start < 0;
  $("#video-empty").hidden = Boolean(fileId);
  if (state.activePreview === "query") {
    const [rangeStart, rangeEnd] = previewRange(
      state.queryMode === "segment" ? start : start - 5,
      state.queryMode === "segment" ? end : end + 5,
      player,
    );
    $("#active-range-label").textContent = `${fmtTime(rangeStart)} – ${fmtTime(rangeEnd)}`;
    $("#query-range-label").textContent = `${fmtTime(rangeStart)} – ${fmtTime(rangeEnd)}`;
  }
  if (fileId && player.readyState >= 1 && Number.isFinite(player.duration)) {
    $("#query-start").max = String(Math.max(0, player.duration - 2));
  }
}

$("#query-file").addEventListener("change", updateQueryPreview);
$("#query-file").addEventListener("change", () => renderJob(state.job));
$("#query-start").addEventListener("input", updateQueryPreview);
$("#query-player").addEventListener("loadedmetadata", updateQueryPreview);
$("#candidate-player").addEventListener("loadedmetadata", () => {
  if (state.activePreview === "candidate") updateActivePreview();
});
$("#engine").addEventListener("change", () => {
  state.engineTouched = true;
  renderResultProvenance();
  renderJob(state.job);
});
$("#query-context").addEventListener("click", () => {
  const start = Number($("#query-start").value);
  const end = start + 2;
  state.queryMode = state.queryMode === "segment" ? "context" : "segment";
  $("#query-context").textContent = state.queryMode === "segment" ? "前後各5秒を見る" : "2秒見本だけを見る";
  const [rangeStart, rangeEnd] = previewRange(
    state.queryMode === "segment" ? start : start - 5,
    state.queryMode === "segment" ? end : end + 5,
    $("#query-player"),
  );
  applyRange($("#query-player"), rangeStart, rangeEnd);
  updateActivePreview();
});

function renderResults(results, provenance = undefined) {
  state.results = results || [];
  if (provenance !== undefined) state.resultProvenance = provenance;
  renderResultProvenance();
  const list = $("#results-list");
  list.replaceChildren();
  $("#result-count").textContent = `${state.results.length}件`;
  if (!state.results.length) {
    empty(list, state.resultProvenance ? "この検索方式の索引候補がありません。検索方式と索引設定を確認してください。" : "2秒の見本を指定して検索してください。");
    state.selectedCandidate = null;
    $("#candidate-view-tab").disabled = true;
    showPreview("query");
    return;
  }
  list.classList.remove("empty-state");
  for (const result of state.results) {
    const card = document.createElement("article");
    card.className = "match-card";
    card.dataset.candidateId = result.id;
    const main = document.createElement("div");
    main.className = "match-main";
    const title = document.createElement("div");
    title.className = "match-title";
    title.textContent = result.name;
    const time = document.createElement("div");
    time.className = "match-time";
    time.textContent = `${fmtTime(result.start_s)} – ${fmtTime(result.end_s)}  ·  元動画の時刻`;
    main.append(title, time);
    const score = document.createElement("div");
    score.className = "match-score";
    score.textContent = `類似度 ${Number(result.score).toFixed(3)}`;
    const actions = document.createElement("div");
    actions.className = "match-actions";
    const compare = document.createElement("button");
    compare.className = "button button-mini button-compare";
    compare.type = "button";
    compare.textContent = result.preview_available ? "見本と比較" : "この画面で動画を選択して索引すると再生できます";
    compare.disabled = !result.preview_available;
    compare.addEventListener("click", () => selectCandidate(result));
    actions.append(compare);
    for (const [decision, label, css] of [["keep", "採用", "button-keep"], ["hold", "保留", "button-hold"]]) {
      const button = document.createElement("button");
      button.className = `button button-mini ${css}`;
      button.type = "button";
      button.textContent = label;
      button.disabled = !result.preview_available || Boolean(result.decision);
      button.addEventListener("click", () => saveDecision(result, decision));
      actions.append(button);
    }
    card.append(main, score, actions);
    list.append(card);
  }
  const topPreview = state.results.find((result) => result.preview_available);
  if (topPreview) selectCandidate(topPreview, false);
}

function selectCandidate(result, switchView = true) {
  if (!result.preview_available || !result.file_id) return;
  state.selectedCandidate = result;
  for (const card of document.querySelectorAll(".match-card")) card.classList.toggle("selected", card.dataset.candidateId === result.id);
  $("#candidate-view-tab").disabled = false;
  setPlayerSource($("#candidate-player"), result.file_id);
  state.candidateMode = "segment";
  $("#candidate-context").textContent = "前後各5秒を見る";
  $("#candidate-context").disabled = false;
  if (switchView) showPreview("candidate");
  else if (state.activePreview === "candidate") updateActivePreview();
  applyRange($("#candidate-player"), result.start_s, result.end_s, false);
}

$("#candidate-context").addEventListener("click", () => {
  const result = state.selectedCandidate;
  if (!result) return;
  state.candidateMode = state.candidateMode === "segment" ? "context" : "segment";
  const [start, end] = previewRange(
    state.candidateMode === "segment" ? result.start_s : result.start_s - 5,
    state.candidateMode === "segment" ? result.end_s : result.end_s + 5,
    $("#candidate-player"),
  );
  if (state.activePreview === "candidate") $("#active-range-label").textContent = `${fmtTime(start)} – ${fmtTime(end)}`;
  $("#candidate-context").textContent = state.candidateMode === "segment" ? "前後各5秒を見る" : "2秒見本だけを見る";
  applyRange($("#candidate-player"), start, end, false);
  updateActivePreview();
});

async function saveDecision(result, decision) {
  if (!result.preview_available || result.decision || state.pendingDecisions.has(result.id)) return;
  state.pendingDecisions.add(result.id);
  renderResults(state.results);
  try {
    await api("/api/decision", { method: "POST", body: { candidate_id: result.id, decision } });
    result.decision = decision;
    toast(decision === "keep" ? "採用リストに保存しました" : "保留リストに保存しました");
  } catch (error) { toast(error.message); }
  finally {
    state.pendingDecisions.delete(result.id);
    renderResults(state.results);
    await refreshReferences();
  }
}

function isBusy(job) { return job && ["queued", "running", "cancelling"].includes(job.status); }

function renderJob(job) {
  state.job = job;
  if (job && ["index", "search"].includes(job.kind) && job.engine && !state.engineTouched) {
    $("#engine").value = job.engine;
    renderResultProvenance();
  }
  const busy = isBusy(job);
  $("#start-index").disabled = busy || state.files.length === 0 || state.indexSubmitting;
  const hasQuery = Boolean($("#query-file").value);
  const indexed = queryFileIndexedForSelectedEngine();
  const preparePanel = $("#library-settings");
  $("#prepare-ready").textContent = !hasQuery ? "動画を選択してください" : indexed ? "検索の準備完了" : "この方式では未索引";
  preparePanel.classList.toggle("is-prepared", indexed);
  if (job?.kind === "index" && job.status === "complete" && indexed) preparePanel.open = false;
  else if (!busy && hasQuery && !indexed) preparePanel.open = true;
  $("#start-search").setAttribute("aria-disabled", String(busy || !hasQuery || !indexed || state.searchSubmitting));
  const notice = $("#query-index-notice");
  notice.hidden = false;
  if (!hasQuery) notice.textContent = "「動画・索引」から動画を選ぶと検索できます。";
  else if (busy && job?.kind === "index") notice.textContent = "索引の完了後に検索できます。";
  else if (!indexed) notice.textContent = engineName($("#engine").value) + "ではこの動画が未索引です。「索引を作成 / 再開」を完了してください。";
  else { notice.textContent = ""; notice.hidden = true; }
  $("#cancel-job").disabled = !busy || job?.status === "cancelling" || job?.owned === false || state.cancelSubmitting;
  $("#job-dot").classList.toggle("is-busy", Boolean(busy));
  $("#job-dot").classList.toggle("is-error", job?.status === "failed");
  $("#job-strip").dataset.status = job?.status || "idle";
  if (!job) {
    $("#job-status").textContent = "準備完了。動画を選択してください。";
    $("#job-progress").value = 0;
    $("#job-progress").max = 1;
    $("#progress-caption").textContent = "待機中";
    return;
  }
  $("#job-status").textContent = job.error || job.message || job.stage || "処理中";
  const total = Number(job.total) || 0;
  const done = Number(job.done) || 0;
  $("#job-progress").max = Math.max(1, total);
  $("#job-progress").value = Math.min(done, Math.max(1, total));
  $("#progress-caption").textContent = job.owned === false ? "前の画面セッションの処理です。完了後に素材を選び直せます。" : total ? `${job.current_name || "動画"} · ${done} / ${total} windows${job.device ? ` · ${job.device}` : ""}` : (job.kind === "search" ? "モデル起動・検索中" : "モデル起動中") + (job.current_name ? ` · ${job.current_name}` : "");
  if (job.kind === "search" && job.status === "complete") {
    renderResults(job.results || [], { engine: job.engine, fingerprint: job.fingerprint });
  }
  if (job.kind === "search" && ["queued", "running", "cancelling"].includes(job.status)) {
    state.resultProvenance = { engine: job.engine, fingerprint: job.fingerprint };
    renderResultProvenance();
  }
  if (job.kind === "search" && job.status === "cancelled") renderResults([], { engine: job.engine, fingerprint: job.fingerprint });
  if (job.kind === "search" && job.status === "failed") renderResults([], { engine: job.engine, fingerprint: job.fingerprint });
}

async function refreshReferences() {
  const sequence = ++state.referenceSequence;
  try {
    const data = await api("/api/references");
    if (sequence !== state.referenceSequence) return;
    const root = $("#references-list");
    $("#reference-count").textContent = String(data.references.length);
    root.replaceChildren();
    if (!data.references.length) {
      empty(root, "採用または保留した候補がここにまとまります。");
      return;
    }
    root.classList.remove("empty-state");
    for (const ref of data.references) {
      const row = document.createElement("div");
      row.className = "reference-row";
      const name = document.createElement("span");
      name.className = "ref-name";
      name.textContent = `${ref.name} · ${fmtTime(ref.start_s)}–${fmtTime(ref.end_s)}`;
      const decision = document.createElement("span");
      decision.className = `decision-pill${ref.decision === "hold" ? " hold" : ""}`;
      decision.textContent = ref.decision === "keep" ? "採用" : "保留";
      const id = document.createElement("span");
      id.className = "match-time";
      id.textContent = ref.pipeline.slice(0, 20);
      row.append(name, decision, id);
      root.append(row);
    }
  } catch { /* A missing index has an empty reference list. */ }
}

async function refreshState() {
  const sequence = ++state.stateSequence;
  try {
    const data = await api("/api/state");
    if (sequence !== state.stateSequence || state.searchSubmitting || state.indexSubmitting) return;
    if (state.job && isBusy(state.job) && data.job?.id !== state.job.id) return;
    const shape = (files) => JSON.stringify(files.map((f) => [f.id, f.indexed, f.indexed_pipelines]));
    const changed = shape(state.files) !== shape(data.files);
    state.files = data.files;
    if (changed) renderFiles();
    renderStoreReadiness(data);
    renderJob(data.job);
  } catch (error) { toast(error.message); }
  await refreshReferences();
}

async function pollJob() {
  const expectedId = state.job?.id;
  const sequence = ++state.pollSequence;
  try {
    const data = await api("/api/job");
    if (sequence !== state.pollSequence || !expectedId || state.job?.id !== expectedId || data.job?.id !== expectedId) return;
    renderJob(data.job);
    if (!isBusy(data.job)) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = 0;
      if (data.job?.kind === "index" && ["complete", "cancelled", "failed"].includes(data.job.status)) {
        const latest = await api("/api/state");
        if (state.job?.id !== expectedId) return;
        state.files = latest.files;
        renderStoreReadiness(latest);
        renderFiles();
        renderJob(state.job);
      }
      await refreshReferences();
    }
  } catch (error) { toast(error.message); }
}

function beginPolling() {
  if (!state.pollTimer) state.pollTimer = window.setInterval(pollJob, 700);
}

$("#add-video").addEventListener("click", async () => {
  try {
    const data = await api("/api/select-video", { method: "POST", body: {} });
    if (data.files.length) {
      const current = new Map(state.files.map((f) => [f.id, f]));
      for (const file of data.files) current.set(file.id, file);
      state.files = Array.from(current.values());
      renderFiles();
      updateQueryPreview();
      renderJob(state.job);
      toast("動画をローカルライブラリに追加しました");
    }
  } catch (error) { toast(error.message); }
});

$("#choose-store").addEventListener("click", async () => {
  try {
    const data = await api("/api/select-store", { method: "POST", body: {} });
    $("#store-label").textContent = data.store_label;
    await refreshState();
    toast("索引の保存先を設定しました");
  } catch (error) { toast(error.message); }
});

$("#start-index").addEventListener("click", async () => {
  if (state.indexSubmitting || isBusy(state.job)) return;
  const file_ids = Array.from(document.querySelectorAll("#library-list input:checked"), (input) => input.value);
  state.indexSubmitting = true;
  state.stateSequence += 1;
  $("#start-index").disabled = true;
  renderResults([], null);
  try {
    const data = await api("/api/index", { method: "POST", body: { file_ids, engine: $("#engine").value } });
    renderJob(data.job);
    beginPolling();
  } catch (error) { toast(error.message); }
  finally {
    state.indexSubmitting = false;
    renderJob(state.job);
  }
});

$("#cancel-job").addEventListener("click", async () => {
  if (state.cancelSubmitting || !isBusy(state.job)) return;
  const expectedId = state.job.id;
  state.cancelSubmitting = true;
  $("#cancel-job").disabled = true;
  try {
    const data = await api("/api/job/cancel", { method: "POST", body: {} });
    if (state.job?.id !== expectedId || data.job?.id !== expectedId) return;
    renderJob(data.job);
    beginPolling();
  } catch (error) { toast(error.message); }
  finally {
    state.cancelSubmitting = false;
    if (state.job?.id === expectedId) renderJob(state.job);
  }
});

$("#start-search").addEventListener("click", async () => {
  if (state.searchSubmitting || isBusy(state.job)) return;
  const file_id = $("#query-file").value;
  const start = Number($("#query-start").value);
  if (!file_id) {
    toast("先に「動画・索引の設定」で動画を選んでください");
    return;
  }
  if (!queryFileIndexedForSelectedEngine()) {
    toast(engineName($("#engine").value) + "で先に動画を索引してください");
    return;
  }
  if (!Number.isFinite(start) || start < 0) {
    toast("0秒以上の見本開始時刻を指定してください");
    return;
  }
  const sequence = ++state.searchSequence;
  state.stateSequence += 1;
  state.searchSubmitting = true;
  $("#start-search").setAttribute("aria-disabled", "true");
  showPreview("query");
  renderResults([], null);
  try {
    const data = await api("/api/search", { method: "POST", body: { file_id, start, engine: $("#engine").value } });
    if (sequence !== state.searchSequence) return;
    renderJob(data.job);
    beginPolling();
  } catch (error) {
    if (sequence === state.searchSequence) toast(error.message);
  } finally {
    if (sequence === state.searchSequence) {
      state.searchSubmitting = false;
      renderJob(state.job);
    }
  }
});

$("#export-references").addEventListener("click", (event) => {
  if ($("#export-references").getAttribute("aria-disabled") === "true") {
    event.preventDefault();
    toast("参照リストを書き出すには、先に動画を索引して候補を採用または保留してください");
  }
});

async function boot() {
  await refreshState();
  if (isBusy(state.job)) beginPolling();
  const chosen = $("#query-file").value;
  if (chosen) updateQueryPreview();
}

boot();
