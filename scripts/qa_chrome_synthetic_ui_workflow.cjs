/* Real Chrome UI input against the temporary synthetic-only workflow service. */
const fs = require("node:fs/promises");
const path = require("node:path");

async function main() {
  const debugPort = Number(process.argv[2]);
  const downloadDir = process.argv[3];
  const lightOnly = process.argv.includes("--light-only");
  if (!Number.isInteger(debugPort) || debugPort < 1 || !downloadDir) {
    throw new Error("usage: node qa_chrome_synthetic_ui_workflow.cjs DEBUG_PORT DOWNLOAD_DIR");
  }

  const version = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).json();
  const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  const target = targets.find((item) => item.type === "page" && item.url.includes("127.0.0.1:8768/"));
  if (!target) throw new Error("isolated synthetic Chrome page not found");
  let browserSocket;
  const downloadEvents = [];

  async function setDownloadDirectory() {
    browserSocket = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      browserSocket.addEventListener("open", resolve, { once: true });
      browserSocket.addEventListener("error", reject, { once: true });
    });
    browserSocket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.method === "Browser.downloadWillBegin" || message.method === "Browser.downloadProgress") downloadEvents.push(message.method);
    });
    browserSocket.send(JSON.stringify({ id: 1, method: "Browser.setDownloadBehavior", params: { behavior: "allow", downloadPath: downloadDir, eventsEnabled: true } }));
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Chrome download directory setup timed out")), 5000);
      browserSocket.addEventListener("message", (event) => {
        const message = JSON.parse(event.data);
        if (message.id === 1) {
          clearTimeout(timer);
          message.error ? reject(new Error(message.error.message)) : resolve();
        }
      });
    });
  }
  await setDownloadDirectory();

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  let sequence = 0;
  const pending = new Map();
  const exportRequests = [];
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.requestWillBeSent" && new URL(message.params.request.url).pathname === "/api/references/export") exportRequests.push({ event: "request", method: message.params.request.method });
    if (message.method === "Network.responseReceived" && new URL(message.params.response.url).pathname === "/api/references/export") exportRequests.push({ event: "response", status: message.params.response.status, mimeType: message.params.response.mimeType });
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id);
    pending.delete(message.id);
    message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result);
  });
  function send(method, params = {}) {
    const id = ++sequence;
    ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");

  async function evaluate(expression, awaitPromise = true) {
    let lastError;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      try {
        const result = await send("Runtime.evaluate", { expression, awaitPromise, returnByValue: true, userGesture: false });
        if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "page script failed");
        return result.result.value;
      } catch (error) {
        lastError = error;
        if (!String(error.message).includes("Execution context was destroyed")) throw error;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
    throw lastError || new Error("page execution context did not stabilize");
  }
  async function key(key, { code = key, modifiers = 0, text = "" } = {}) {
    await send("Input.dispatchKeyEvent", { type: "keyDown", key, code, modifiers, text });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, modifiers });
  }
  async function clickAt(rect) {
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  }
  async function bringIntoView(selector) {
    for (let count = 0; count < 12; count += 1) {
      const rect = await evaluate(`(() => {const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,innerHeight}})()`);
      if (rect.y >= 0 && rect.y + rect.height <= rect.innerHeight) return rect;
      await send("Input.dispatchMouseEvent", { type: "mouseWheel", x: Math.max(10, rect.x + 10), y: Math.min(rect.innerHeight - 5, Math.max(5, rect.innerHeight / 2)), deltaX: 0, deltaY: rect.y < 0 ? -500 : 500 });
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(`could not scroll ${selector} into view`);
  }
  async function clickElement(selector) {
    const rect = await bringIntoView(selector);
    await clickAt(rect);
  }
  async function selectNext(selector, direction) {
    await clickElement(selector);
    const letter = direction < 0 ? "v" : "c";
    await key(letter, { code: `Key${letter.toUpperCase()}`, text: letter });
    await key("Enter");
  }
  async function waitFor(expression, timeoutMs, description) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      const value = await evaluate(expression);
      if (value) return value;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    const current = await evaluate(`(() => {const v=document.querySelector('#candidate-player');return {status:document.querySelector('#job-status')?.textContent||'',job:document.querySelector('#job-strip')?.dataset.status||'',engine:document.querySelector('#engine')?.value||'',queryStart:document.querySelector('#query-start')?.value||'',active:document.activeElement?.id||'',cards:document.querySelectorAll('.match-card').length,candidate:{time:v?.currentTime,paused:v?.paused,readyState:v?.readyState,width:v?.videoWidth,height:v?.videoHeight,error:v?.error?.code||null,viewHidden:document.querySelector('#candidate-view')?.hidden,contextText:document.querySelector('#candidate-context')?.textContent,contextDisabled:document.querySelector('#candidate-context')?.disabled,status:document.querySelector('#candidate-playback-status')?.textContent,events:v?.__qaEvents||[]}}})()`);
    throw new Error(`${description} timed out: ${JSON.stringify(current)}`);
  }
  async function selectFile() {
    await clickElement("#query-file");
    await key("ArrowDown");
    await key("Enter");
    return waitFor("document.querySelector('#query-file').value !== ''", 5000, "video selection");
  }
  async function setQueryTime(value) {
    const initial = Number(await evaluate("document.querySelector('#query-start').value"));
    if (initial === value) return;
    await clickElement("#query-start");
    const direction = value > initial ? "ArrowUp" : "ArrowDown";
    const count = Math.round(Math.abs(value - initial) / 0.5);
    for (let index = 0; index < count; index += 1) {
      await send("Input.dispatchKeyEvent", { type: "keyDown", key: direction, code: direction, windowsVirtualKeyCode: direction === "ArrowUp" ? 38 : 40, nativeVirtualKeyCode: direction === "ArrowUp" ? 126 : 125 });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key: direction, code: direction, windowsVirtualKeyCode: direction === "ArrowUp" ? 38 : 40, nativeVirtualKeyCode: direction === "ArrowUp" ? 126 : 125 });
    }
    await key("Tab");
    await waitFor(`Number(document.querySelector('#query-start').value) === ${value}`, 3000, "query start time");
  }
  async function clickPlayerPlay(selector) {
    await bringIntoView(selector);
    const rect = await evaluate(`(() => {const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})()`);
    const x = rect.x + Math.min(42, rect.width * 0.12);
    const y = rect.y + rect.height - 30;
    const hit = await evaluate(`(() => {const e=document.elementFromPoint(${x},${y});return window.__qaLastHit={tag:e?.tagName||null,id:e?.id||null,className:typeof e?.className==='string'?e.className:'',point:{x:${x},y:${y}},rect:${JSON.stringify(rect)}}})()`);
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    return hit;
  }
  async function awaitJob(kind, maxMs = 240000) {
    const start = Date.now();
    const previousMessage = await evaluate("document.querySelector('#job-status').textContent");
    await waitFor(`(() => {const s=document.querySelector('#job-strip').dataset.status;const k=document.querySelector('#job-status').textContent;return ['queued','running','cancelling'].includes(s)||k!==${JSON.stringify(previousMessage)} })()`, 10000, `${kind} job start`);
    const done = await waitFor(`(() => {const s=document.querySelector('#job-strip').dataset.status;const k=document.querySelector('#job-status').textContent;return ['complete','failed','cancelled'].includes(s)&&k!==${JSON.stringify(previousMessage)}?{status:s,message:k,engine:document.querySelector('#engine').value,elapsed:(Date.now()-${start})/1000}:null})()`, maxMs, `${kind} job`);
    if (done.status !== "complete") throw new Error(`${kind} job failed: ${JSON.stringify(done)}`);
    return done;
  }
  async function waitPlayer(selector) {
    return waitFor(`(() => {const v=document.querySelector(${JSON.stringify(selector)});return v.currentSrc && v.readyState>=2?{time:v.currentTime,readyState:v.readyState,width:v.videoWidth,height:v.videoHeight,paused:v.paused}:null})()`, 20000, `${selector} decode`);
  }
  async function playAndMeasure(selector) {
    const before = await waitPlayer(selector);
    await clickPlayerPlay(selector);
    const after = await waitFor(`(() => {const v=document.querySelector(${JSON.stringify(selector)});return !v.paused && v.currentTime > ${before.time} + 0.3 ? {time:v.currentTime,paused:v.paused,width:v.videoWidth,height:v.videoHeight,error:v.error?.code||null} : null})()`, 5000, `${selector} playback`);
    return { before, after };
  }
  async function restartAndPlayQuery() {
    const before = await waitPlayer("#query-player");
    await clickElement("#query-context");
    await clickElement("#query-context");
    const after = await waitFor(`(() => {const v=document.querySelector('#query-player');return !v.paused && v.currentTime > 1.3 ? {time:v.currentTime,paused:v.paused,width:v.videoWidth,height:v.videoHeight,error:v.error?.code||null}:null})()`, 5000, "query playback from the selected 2-second range");
    return { before, after, trigger: "browser click on context and segment buttons; both play the selected query range" };
  }
  async function restartAndPlayCandidate() {
    const before = await waitPlayer("#candidate-player");
    await evaluate("(() => {const v=document.querySelector('#candidate-player');v.__qaEvents=[];window.__qaDomEvents=[];for(const type of ['play','pause','timeupdate'])v.addEventListener(type,()=>v.__qaEvents.push({type,time:Number(v.currentTime.toFixed(3))}));for(const type of ['pointerdown','pointerup','click'])document.addEventListener(type,e=>window.__qaDomEvents.push({type,target:e.target?.tagName||null,id:e.target?.id||null,className:typeof e.target?.className==='string'?e.target.className:'',trusted:e.isTrusted,x:e.clientX,y:e.clientY,userActive:navigator.userActivation?.isActive||false}),true);return true})()");
    await clickElement("#candidate-context");
    await clickElement("#candidate-context");
    await new Promise((resolve) => setTimeout(resolve, 300));
    const afterContext = await evaluate("(() => {const v=document.querySelector('#candidate-player');return {time:v.currentTime,paused:v.paused}})()");
    let trigger = "context/segment controls started playback";
    let playControlHit = null;
    if (afterContext.paused) {
      playControlHit = await clickPlayerPlay("#candidate-player");
      if (playControlHit.tag !== "VIDEO" || playControlHit.id !== "candidate-player") {
        throw new Error(`candidate native control point hit ${playControlHit.tag}#${playControlHit.id} instead of the video`);
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
      const afterNative = await evaluate("(() => {const v=document.querySelector('#candidate-player');return {time:v.currentTime,paused:v.paused}})()");
      if (afterNative.paused) {
        const rect = await bringIntoView("#candidate-player");
        await clickAt(rect);
        await new Promise((resolve) => setTimeout(resolve, 200));
        const afterSurface = await evaluate("(() => {const v=document.querySelector('#candidate-player');return {time:v.currentTime,paused:v.paused}})()");
        if (afterSurface.paused) {
          await send("Input.dispatchKeyEvent", { type: "keyDown", key: " ", code: "Space", windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49 });
          await send("Input.dispatchKeyEvent", { type: "keyUp", key: " ", code: "Space", windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49 });
          trigger = "context/segment, native play control, video surface, and Space key browser input";
        } else trigger = "context/segment and video surface browser input";
      } else trigger = "context/segment and native play control browser input";
    }
    const after = await waitFor(`(() => {const v=document.querySelector('#candidate-player');return !v.paused && v.currentTime > ${before.time} + 0.2 ? {time:v.currentTime,paused:v.paused,width:v.videoWidth,height:v.videoHeight,error:v.error?.code||null,events:v.__qaEvents}:null})()`, 5000, "candidate playback from the selected 2-second range");
    const domEvents = await evaluate("window.__qaDomEvents");
    return { before, after, afterContext, trigger, playControlHit, domEvents };
  }
  async function playToEndAndReplay(selector) {
    const range = await evaluate(`(() => {const v=document.querySelector(${JSON.stringify(selector)});return {start:Number(v.dataset.rangeStart),end:Number(v.dataset.rangeEnd),time:v.currentTime,paused:v.paused}})()`);
    const ended = await waitFor(`(() => {const v=document.querySelector(${JSON.stringify(selector)});const end=Number(v.dataset.rangeEnd);return v.paused && Number.isFinite(end) && v.currentTime >= end-0.06 ? {time:v.currentTime,paused:v.paused,end}:null})()`, 6000, `${selector} selected segment end`);
    const playHit = await clickPlayerPlay(selector);
    const deadline = Date.now() + 2500;
    let replay = null;
    while (Date.now() < deadline) {
      replay = await evaluate(`(() => {const v=document.querySelector(${JSON.stringify(selector)});return {time:v.currentTime,paused:v.paused,readyState:v.readyState,error:v.error?.code||null}})()`);
      if (!replay.paused && replay.time > range.start + 0.2 && replay.time < range.end - 0.1) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return { range, ended, replay, restarted: Boolean(replay && !replay.paused && replay.time > range.start + 0.2 && replay.time < range.end - 0.1), playHit };
  }
  async function compareTabsAndContext() {
    await clickElement("#query-view-tab");
    await waitFor("!document.querySelector('#query-view').hidden && document.querySelector('#candidate-view').hidden", 3000, "query tab selection");
    const queryPlayback = await restartAndPlayQuery();
    const queryEndReplay = await playToEndAndReplay("#query-player");
    if (!queryEndReplay.restarted) throw new Error(`query player did not restart from the selected range start: ${JSON.stringify(queryEndReplay)}`);
    await clickElement("#candidate-view-tab");
    await waitFor("!document.querySelector('#candidate-view').hidden", 3000, "candidate tab selection");
    await clickElement(".match-card:nth-child(2) .button-compare");
    const secondCandidateStart = await waitFor("(() => {const v=document.querySelector('#candidate-player');const start=Number(v.dataset.rangeStart);return Number.isFinite(start)&&Math.abs(v.currentTime-start)<0.1?start:null})()", 5000, "second candidate seek");
    const contextButton = await bringIntoView("#candidate-context");
    await clickAt(contextButton);
    await waitFor("document.querySelector('#candidate-context').textContent === '2秒見本だけを見る'", 3000, "candidate context range");
    const contextLabel = await evaluate("document.querySelector('#active-range-label').textContent");
    const segmentButton = await bringIntoView("#candidate-context");
    await clickAt(segmentButton);
    await waitFor("document.querySelector('#candidate-context').textContent === '前後各5秒を見る'", 3000, "candidate segment range");
    await clickElement("#query-view-tab");
    await waitFor("!document.querySelector('#query-view').hidden", 3000, "query tab after context");
    await clickElement("#candidate-view-tab");
    await waitFor("!document.querySelector('#candidate-view').hidden", 3000, "candidate tab after context");
    return { queryPlayback, queryEndReplay, contextLabel, secondCandidateStart, finalTab: "candidate" };
  }
  async function runEngine(engine, first) {
    if (first) {
      await clickElement("#choose-store");
      await waitFor("document.querySelector('#store-label').textContent === 'store'", 5000, "temporary store selection");
      await clickElement("#add-video");
      await waitFor("document.querySelectorAll('#query-file option').length > 1", 5000, "synthetic video picker substitute");
      await selectFile();
    } else if (!(await evaluate("document.querySelector('#library-settings').open"))) {
      await clickElement("#library-settings > summary");
    }
    const selectedEngine = await evaluate("document.querySelector('#engine').value");
    if (selectedEngine !== engine) await selectNext("#engine", engine === "color-motion" ? 1 : -1);
    await waitFor(`document.querySelector('#engine').value === ${JSON.stringify(engine)}`, 3000, `${engine} mode selection`);
    await clickElement("#start-index");
    const index = await awaitJob(`${engine} indexing`);
    await selectFile();
    const emptyOverlayHidden = await evaluate("document.querySelector('#video-empty').hidden");
    if (!emptyOverlayHidden) throw new Error(`${engine} indexing refresh left the empty-state overlay above the selected video`);
    await setQueryTime(1);
    await waitPlayer("#query-player");
    const queryPlayback = await restartAndPlayQuery();
    await clickElement("#start-search");
    const search = await awaitJob(`${engine} search`);
    const resultCount = await waitFor("document.querySelectorAll('.match-card').length", 10000, `${engine} results`);
    if (resultCount < 2) throw new Error(`${engine} produced fewer than 2 candidate rows`);
    await clickElement(".match-card .button-compare");
    await waitFor("!document.querySelector('#candidate-view').hidden", 5000, `${engine} candidate comparison view`);
    await waitPlayer("#candidate-player");
    let candidatePlayback;
    try {
      candidatePlayback = await restartAndPlayCandidate();
    } catch (error) {
      if (engine !== "vjepa2") throw error;
      const observed = await evaluate("(() => {const v=document.querySelector('#candidate-player');return {time:v.currentTime,paused:v.paused,readyState:v.readyState,width:v.videoWidth,height:v.videoHeight,error:v.error?.code||null,playEvents:v.__qaEvents||[],domEvents:window.__qaDomEvents||[],playControlHit:window.__qaLastHit||null}})()");
      candidatePlayback = { ok: false, error: String(error.message), observed };
    }
    const candidateEndReplay = await playToEndAndReplay("#candidate-player");
    if (!candidateEndReplay.restarted) throw new Error(`candidate player did not restart from the selected range start: ${JSON.stringify(candidateEndReplay)}`);
    const tabAndContext = await compareTabsAndContext();
    const baseReferenceCount = Number(await evaluate("document.querySelector('#reference-count').textContent"));
    await clickElement(".match-card:nth-child(1) .button-keep");
    await waitFor(`document.querySelector('#reference-count').textContent === ${JSON.stringify(String(baseReferenceCount + 1))}`, 5000, `${engine} keep decision`);
    await clickElement(".match-card:nth-child(2) .button-hold");
    await waitFor(`document.querySelector('#reference-count').textContent === ${JSON.stringify(String(baseReferenceCount + 2))}`, 5000, `${engine} hold decision`);
    return { engine, index, search, resultCount, queryPlayback, candidatePlayback, candidateEndReplay, tabAndContext, references: 2 };
  }

  try {
    await send("Page.reload", { ignoreCache: true });
    await waitFor("document.readyState === 'complete' && document.querySelector('#start-index')", 10000, "app boot");
    const light = await runEngine("color-motion", true);
    const jepa = lightOnly ? null : await runEngine("vjepa2", false);
    const count = await evaluate("document.querySelector('#reference-count').textContent");
    const expectedRows = lightOnly ? 2 : 4;
    if (count !== String(expectedRows)) throw new Error(`expected ${expectedRows} saved references; got ${count}`);
    const exportState = await evaluate("(() => {const a=document.querySelector('#export-references');return {disabled:a.getAttribute('aria-disabled'),href:a.getAttribute('href'),count:document.querySelector('#reference-count').textContent}})()");
    if (exportState.disabled !== "false") throw new Error(`reference export link was disabled: ${JSON.stringify(exportState)}`);
    await clickElement("#export-references");
    let exportPath = null;
    const deadline = Date.now() + 12000;
    while (Date.now() < deadline) {
      const files = await fs.readdir(downloadDir);
      const candidate = files.find((name) => name === "references.json" || name.endsWith(".json"));
      if (candidate) { exportPath = path.join(downloadDir, candidate); break; }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!exportPath) throw new Error(`references JSON did not download; files=${JSON.stringify(await fs.readdir(downloadDir))}; network=${JSON.stringify(exportRequests)}; browser=${JSON.stringify(downloadEvents)}; link=${JSON.stringify(exportState)}`);
    const exported = JSON.parse(await fs.readFile(exportPath, "utf8"));
    const rows = exported.references || exported;
    if (!Array.isArray(rows) || rows.length !== expectedRows) throw new Error(`expected ${expectedRows} exported JSON rows; got ${Array.isArray(rows) ? rows.length : "invalid"}`);
    console.log(JSON.stringify({ browser: version.Browser, title: await evaluate("document.title"), picker: "test substitute returned only the H.264 synthetic fixture; native file dialog was not opened", browserInput: "CDP mouse/keyboard input used the two-step preparation card, synthetic picker/store, engine selects, indexing/search, query/candidate playback controls, compare/decision buttons, and JSON link", colorMotion: light, vjepa2: jepa, export: { rows: rows.length, bytes: (await fs.stat(exportPath)).size, decisions: rows.map((row) => row.decision).sort() } }, null, 2));
  } finally {
    ws.close();
    try {
      browserSocket.send(JSON.stringify({ id: 1, method: "Browser.close" }));
    } catch { /* The dedicated test browser may already have closed. */ }
  }
}

main().catch((error) => { console.error(error.stack || String(error)); process.exitCode = 1; });
