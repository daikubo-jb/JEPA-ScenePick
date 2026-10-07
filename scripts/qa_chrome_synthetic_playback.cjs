/* Drive only the dedicated synthetic Chrome profile; never use the user's profile. */
async function main() {
const debugPort = Number(process.argv[2]);
if (!Number.isInteger(debugPort) || debugPort < 1) throw new Error("pass the ephemeral Chrome DevTools port");

const version = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).json();
async function closeIsolatedBrowser() {
  const browserSocket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    browserSocket.addEventListener("open", resolve, { once: true });
    browserSocket.addEventListener("error", reject, { once: true });
  });
  browserSocket.send(JSON.stringify({ id: 1, method: "Browser.close" }));
}
const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
const target = targets.find((item) => item.type === "page" && item.url.includes("127.0.0.1:8768/"));
if (!target) throw new Error("isolated synthetic page target not found");
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});

let sequence = 0;
const pending = new Map();
const media = new Map();
ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const item = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) item.reject(new Error(message.error.message));
    else item.resolve(message.result);
    return;
  }
  if (message.method === "Network.requestWillBeSent") {
    const request = message.params.request;
    if (request.url.includes("/media/")) media.set(message.params.requestId, { urlPathOnly: new URL(request.url).pathname });
  } else if (message.method === "Network.requestWillBeSentExtraInfo") {
    const item = media.get(message.params.requestId);
    if (item) {
      item.cookiePresent = Boolean(message.params.headers.Cookie || message.params.headers.cookie);
      item.range = message.params.headers.Range || message.params.headers.range || null;
    }
  } else if (message.method === "Network.responseReceived") {
    const item = media.get(message.params.requestId);
    if (item) {
      item.status = message.params.response.status;
      item.mimeType = message.params.response.mimeType;
      item.contentRange = message.params.response.headers["Content-Range"] || null;
      item.acceptRanges = message.params.response.headers["Accept-Ranges"] || null;
    }
  }
});

function send(method, params = {}, sessionId = undefined) {
  const id = ++sequence;
  const message = { id, method, params };
  if (sessionId) message.sessionId = sessionId;
  ws.send(JSON.stringify(message));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

const sessionId = undefined;
await send("Network.enable", {}, sessionId);
await send("Page.enable", {}, sessionId);
await send("Page.reload", { ignoreCache: true }, sessionId);

async function evaluate(expression, awaitPromise = true) {
  let lastError;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const result = await send("Runtime.evaluate", {
        expression, awaitPromise, returnByValue: true, userGesture: false
      }, sessionId);
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "runtime evaluation failed");
      return result.result.value;
    } catch (error) {
      lastError = error;
      if (!String(error.message).includes("Execution context was destroyed")) throw error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw lastError || new Error("page execution context did not stabilize");
}

const ready = await evaluate(`new Promise((resolve, reject) => {
  const deadline = Date.now() + 15000;
  const tick = () => {
    const video = document.querySelector('#query-player');
    if (video?.currentSrc && video.readyState >= 2 && document.querySelector('.match-card')) {
      resolve({ok:true,src: new URL(video.currentSrc).pathname, readyState: video.readyState,
        duration: video.duration, controls: video.controls, width: video.videoWidth,
        height: video.videoHeight, codecHint: video.canPlayType('video/mp4; codecs="mp4v.20.3"')});
    } else if (Date.now() > deadline) resolve({ok:false,
      queryFile:document.querySelector('#query-file')?.value||null,
      cardCount:document.querySelectorAll('.match-card').length,
      src:video?.currentSrc?new URL(video.currentSrc).pathname:null,
      readyState:video?.readyState??null,networkState:video?.networkState??null,
      duration:video?.duration??null,controls:video?.controls??null,
      width:video?.videoWidth??null,height:video?.videoHeight??null,
      errorCode:video?.error?.code??null,errorMessage:video?.error?.message??null,
      codecHint:video?.canPlayType('video/mp4; codecs="mp4v.20.3"')??null,
      status:document.querySelector('#job-status')?.textContent??null,
      playbackStatus:document.querySelector('#query-playback-status')?.textContent??null});
    else setTimeout(tick, 100);
  };
  tick();
})`);

function safeMediaRequests() {
  return [...media.values()].map(({ urlPathOnly, ...safe }) => ({ urlPathOnly, ...safe }));
}
if (!ready.ok) {
  console.log(JSON.stringify({ browser: version.Browser, pageReady: ready, mediaRequests: safeMediaRequests() }, null, 2));
  ws.close();
  await closeIsolatedBrowser();
  process.exitCode = 1;
  return;
}

async function visibleState(selector) {
  return evaluate(`(() => {const v=document.querySelector(${JSON.stringify(selector)});const r=v.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,before:v.currentTime,readyState:v.readyState,paused:v.paused}})()`);
}

async function clickBrowserControl(rect) {
  const x = rect.x + Math.min(42, rect.width * 0.12);
  const y = rect.y + rect.height - 30;
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y }, sessionId);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 }, sessionId);
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 }, sessionId);
}

async function clickElement(selector) {
  const rect = await evaluate(`(() => {const e=document.querySelector(${JSON.stringify(selector)});const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})()`);
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 }, sessionId);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, button: "left", clickCount: 1 }, sessionId);
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, button: "left", clickCount: 1 }, sessionId);
}

const queryBefore = await visibleState("#query-player");
await clickBrowserControl(queryBefore);
await new Promise((resolve) => setTimeout(resolve, 1400));
const queryAfter = await evaluate(`(() => {const v=document.querySelector('#query-player');let frames=null;try{frames=v.getVideoPlaybackQuality().totalVideoFrames}catch{}return {time:v.currentTime,paused:v.paused,readyState:v.readyState,width:v.videoWidth,height:v.videoHeight,error:v.error?.code??null,frames}})()`);

await clickElement(".match-card .button-compare");
const candidateReady = await evaluate(`new Promise((resolve, reject) => {
 const deadline=Date.now()+8000; const tick=()=>{const v=document.querySelector('#candidate-player');
 if(!document.querySelector('#candidate-view').hidden && v.currentSrc && v.readyState>=2){resolve(true)}
 else if(Date.now()>deadline)reject(new Error('candidate player did not become visible/ready'));else setTimeout(tick,100)};tick();})`);
const candidateBefore = await visibleState("#candidate-player");
await clickBrowserControl(candidateBefore);
await new Promise((resolve) => setTimeout(resolve, 1400));
const candidateAfter = await evaluate(`(() => {const v=document.querySelector('#candidate-player');let frames=null;try{frames=v.getVideoPlaybackQuality().totalVideoFrames}catch{}return {time:v.currentTime,paused:v.paused,readyState:v.readyState,width:v.videoWidth,height:v.videoHeight,error:v.error?.code??null,frames}})()`);

const mediaRequests = safeMediaRequests();
const output = {
  browser: version.Browser,
  pageReady: ready,
  query: { before: queryBefore, after: queryAfter },
  candidate: { before: candidateBefore, after: candidateAfter },
  mediaRequests
};
console.log(JSON.stringify(output, null, 2));
ws.close();
await closeIsolatedBrowser();

if (queryAfter.time <= queryBefore.before + 0.2 || candidateAfter.time <= candidateBefore.before + 0.2 ||
    queryAfter.width < 1 || candidateAfter.width < 1 || queryAfter.error || candidateAfter.error) process.exitCode = 1;
}

main().catch((error) => { console.error(error.stack || String(error)); process.exitCode = 1; });
