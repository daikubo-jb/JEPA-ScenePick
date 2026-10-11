import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pythonBin = process.env.JEPA_QA_PYTHON || 'python3.13';
const qaId = process.argv[2] === '--qa-id' ? process.argv[3] : '';
if (!/^step-(?:06|07)-try-[0-9]{2}$/.test(qaId)) throw new Error('Pass a unique --qa-id such as step-07-try-01');
const screenshotPath = process.env.JEPA_BROWSER_QA_SCREENSHOT_PATH
  ? path.resolve(process.env.JEPA_BROWSER_QA_SCREENSHOT_PATH)
  : path.join(root, 'screenshots', `browser-qa-${qaId}.png`);
if (fs.existsSync(screenshotPath)) throw new Error(`Refusing to overwrite ${screenshotPath}`);
const videoPath = path.join(root, 'samples', 'synthetic-court-motion-h264.mp4');
if (!fs.existsSync(videoPath)) throw new Error('Synthetic H.264 fixture is missing');
const chromePath = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
if (!fs.existsSync(chromePath)) throw new Error(`Chrome not found at ${chromePath}`);

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'jepa-browser-qa-'));
const chromeProfile = fs.mkdtempSync(path.join(os.tmpdir(), 'jepa-browser-chrome-'));
const downloadDir = path.join(tempRoot, 'downloads');
fs.mkdirSync(downloadDir);
let serverProcess;
let chromeProcess;
let page;
const checks = [];
const log = (name, value) => {
  const item = { check: name, result: value };
  checks.push(item);
  console.log(`${name}: ${JSON.stringify(value)}`);
};

function collectLines(child, onLine) {
  let pending = '';
  for (const stream of [child.stdout, child.stderr]) stream?.on('data', chunk => {
    pending += chunk.toString();
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() || '';
    for (const line of lines) onLine(line);
  });
}

async function waitForLine(child, predicate, timeoutMs, description) {
  return await new Promise((resolve, reject) => {
    const seen = [];
    const timer = setTimeout(() => reject(new Error(`Timed out waiting for ${description}; output=${seen.join(' | ')}`)), timeoutMs);
    collectLines(child, line => {
      seen.push(line);
      if (predicate(line)) {
        clearTimeout(timer);
        resolve(line);
      }
    });
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', code => {
      if (code !== null) { clearTimeout(timer); reject(new Error(`${description} process exited: ${code}`)); }
    });
  });
}

async function connectCDP(webSocketUrl) {
  const ws = new WebSocket(webSocketUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  ws.addEventListener('message', event => {
    const message = JSON.parse(String(event.data));
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    message.error ? waiter.reject(new Error(message.error.message)) : waiter.resolve(message.result);
  });
  return {
    send: (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++nextId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    }),
    close: () => ws.close(),
  };
}

try {
  const pythonRuntime = spawnSync(pythonBin, ['-c', [
    'import sys,cv2,numpy',
    'from importlib.metadata import PackageNotFoundError,version',
    'print("python="+sys.version.split()[0])',
    'print("opencv-python-headless="+version("opencv-python-headless"))',
    'print("opencv-import="+cv2.__version__)',
    'print("numpy-installed="+version("numpy"))',
    'print("numpy-import="+numpy.__version__)',
    'print("numpy-declared=2.2.6")',
    'print("pytest="+version("pytest"))',
    'for name in ("Pillow","torch","transformers"):',
    ' try: print(name+"="+version(name))',
    ' except PackageNotFoundError: print(name+"=not-installed")',
    'print("model=not-loaded; checkpoint bytes not read")',
  ].join('\n')], { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  if (pythonRuntime.status !== 0) throw new Error(`Runtime inspection failed: ${pythonRuntime.stderr}`);
  log('runtime_and_model_boundary', pythonRuntime.stdout.trim().split('\n'));

  const riskRoot = process.env.JEPA_RISK_ROOT || path.resolve(root, '../jepa-risk-detection');
  const modelPython = path.join(riskRoot, '.venv', 'bin', 'python');
  if (fs.existsSync(modelPython)) {
    const modelEnv = {
      ...process.env,
      PYTHONDONTWRITEBYTECODE: '1',
      HF_HOME: path.join(tempRoot, 'hf-offline'),
      HF_HUB_OFFLINE: '1',
      TRANSFORMERS_OFFLINE: '1',
      PYTHONPATH: [path.join(root, '.runtime-deps'), path.join(root, 'src'), path.join(riskRoot, 'src')].join(path.delimiter),
    };
    const modelRuntime = spawnSync(modelPython, ['-m', 'jepasearch.cli', 'runtime'], {
      cwd: root, encoding: 'utf8', timeout: 60000, env: modelEnv,
    });
    if (modelRuntime.status !== 0) throw new Error(`Existing model-venv runtime inspection failed: ${modelRuntime.stderr}`);
    log('existing_model_venv_runtime_only', JSON.parse(modelRuntime.stdout));
  } else {
    log('existing_model_venv_runtime_only', 'not found; no environment was created');
  }

  const serverCode = [
    'import tempfile,time',
    'from pathlib import Path',
    'from jepasearch.core import index_video',
    'from jepasearch.ui import LocalUIService,make_server',
    'root=tempfile.TemporaryDirectory(prefix="jepa-browser-qa-")',
    `video=Path(${JSON.stringify(videoPath)}).resolve(strict=True)`,
    'folder=Path(root.name)',
    'def paced_index(path,store,**kwargs):',
    ' callback=kwargs.get("progress_callback")',
    ' def paced(event):',
    '  if callback: callback(event)',
    '  if event.get("stage")=="indexing": time.sleep(.20)',
    ' kwargs["progress_callback"]=paced',
    ' return index_video(path,store,**kwargs)',
    'service=LocalUIService(picker=lambda kind:[video if kind=="file" else folder],index_function=paced_index)',
    'service.default_store_dir=lambda:folder',
    'server=make_server(0,service=service)',
    'print("PORT="+str(server.server_port),flush=True)',
    'server.serve_forever(poll_interval=.25)',
  ].join('\n');
  serverProcess = spawn(pythonBin, ['-u', '-c', serverCode], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: '.test-deps:.runtime-deps:src', PYTHONDONTWRITEBYTECODE: '1', HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  // OpenCV can take more than fifteen seconds to import in a fresh Python process.
  const serverLine = await waitForLine(serverProcess, line => /^PORT=\d+$/.test(line), 90000, 'synthetic UI server');
  const serverPort = Number(serverLine.slice('PORT='.length));
  const origin = `http://127.0.0.1:${serverPort}`;
  log('ui_server', { origin, bind: '127.0.0.1', picker: path.basename(videoPath), store: 'temporary directory' });

  chromeProcess = spawn(chromePath, [
    '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-sync',
    '--disable-background-networking', '--disable-component-update', '--disable-domain-reliability',
    '--autoplay-policy=no-user-gesture-required', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', `--user-data-dir=${chromeProfile}`, 'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  const chromeLine = await waitForLine(chromeProcess, line => line.includes('DevTools listening on ws://127.0.0.1:'), 20000, 'isolated Chrome');
  const browserSocket = chromeLine.slice(chromeLine.indexOf('ws://'));
  const debugOrigin = new URL(browserSocket).origin.replace(/^ws:/, 'http:');
  let browserVersion;
  for (let i = 0; i < 60; i++) {
    try { browserVersion = await (await fetch(`${debugOrigin}/json/version`)).json(); break; }
    catch { await wait(150); }
  }
  if (!browserVersion) throw new Error('Chrome DevTools endpoint did not start');
  log('browser', { product: browserVersion.Browser, automation: 'CDP via Node built-in WebSocket', isolatedProfile: true });

  const target = await (await fetch(`${debugOrigin}/json/new?${encodeURIComponent(origin)}`, { method: 'PUT' })).json();
  page = await connectCDP(target.webSocketDebuggerUrl);
  await page.send('Page.enable');
  await page.send('Runtime.enable');
  await page.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await page.send('Page.setDownloadBehavior', { behavior: 'allow', downloadPath: downloadDir });

  const evaluate = async expression => {
    const response = await page.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true, timeout: 60000 });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.text || JSON.stringify(response.exceptionDetails));
    return response.result?.value;
  };
  const until = async (expression, seconds = 30) => {
    const deadline = Date.now() + seconds * 1000;
    while (Date.now() < deadline) {
      const value = await evaluate(expression);
      if (value) return value;
      await wait(150);
    }
    throw new Error(`Timed out waiting for: ${expression}`);
  };
  await page.send('Page.navigate', { url: `${origin}/` });
  await until(`document.readyState==='complete'&&!!document.querySelector('#add-video')`);
  await until(`document.querySelector('#engine-readiness').textContent!=='ローカルengineの準備状態を確認しています。'`);
  log('initial_ui_engine', await evaluate(`document.querySelector('#engine').value`));
  await evaluate(`document.querySelector('#add-video').click();true`);
  await until(`document.querySelector('#library-count').textContent==='1本'&&document.querySelector('#query-file').value`);
  const media = await evaluate(`({count:document.querySelector('#library-count').textContent,name:document.querySelector('#library-list .file-name')?.textContent})`);
  if (media.name !== 'synthetic-court-motion-h264.mp4') throw new Error('Unexpected video was added');
  log('synthetic_media_added', media);
  await page.send('Page.reload', { ignoreCache: true });
  await until(`document.readyState==='complete'&&!!document.querySelector('#add-video')&&document.querySelector('#library-count').textContent==='1本'&&!!document.querySelector('#query-file').value`);
  log('reload_retains_session', await evaluate(`({count:document.querySelector('#library-count').textContent,selected:Boolean(document.querySelector('#query-file').value),engine:document.querySelector('#engine').value})`));
  const initialReadiness = await evaluate(`({engine:document.querySelector('#engine').value,jepaDisabled:document.querySelector('#engine option[value="vjepa2"]').disabled,indexDisabled:document.querySelector('#start-index').disabled,message:document.querySelector('#engine-readiness').textContent})`);
  if (initialReadiness.engine !== 'vjepa2' || !initialReadiness.jepaDisabled || !initialReadiness.indexDisabled) throw new Error(`Unready default V-JEPA was not blocked: ${JSON.stringify(initialReadiness)}`);
  log('unready_default_vjepa_blocked', initialReadiness);
  await evaluate(`document.querySelector('#choose-store').click();true`);
  await until(`document.querySelector('#store-label').textContent!=='標準の保存先'`);
  await evaluate(`const e=document.querySelector('#engine');e.value='color-motion';e.dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#start-index').click();true`);
  await until(`Number(document.querySelector('#job-progress').value)>0`, 45);
  log('index_started', await evaluate(`({progress:document.querySelector('#progress-caption').textContent,cancelEnabled:!document.querySelector('#cancel-job').disabled})`));
  await evaluate(`document.querySelector('#cancel-job').click();true`);
  await until(`document.querySelector('#job-status').textContent.includes('停止しました')`, 35);
  log('index_cancelled', await evaluate(`document.querySelector('#job-status').textContent`));
  await evaluate(`document.querySelector('#start-index').click();true`);
  await until(`document.querySelector('#job-status').textContent.includes('完了しました')`, 55);
  await until(`document.querySelector('#library-list .file-state')?.textContent==='索引済み'`, 10);
  log('index_resumed', await evaluate(`({state:document.querySelector('#library-list .file-state').textContent,progress:document.querySelector('#progress-caption').textContent})`));
  await until(`document.querySelector('#query-player').readyState>=1`, 20);
  log('query_h264_decode', await evaluate(`({duration:document.querySelector('#query-player').duration,error:document.querySelector('#query-player').error?.code||null})`));
  await evaluate(`const t=document.querySelector('#query-start');t.value='1';t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#start-search').click();true`);
  const resultCount = await until(`document.querySelectorAll('#results-list .match-card').length`, 35);
  log('baseline_search', await evaluate(`({count:document.querySelectorAll('#results-list .match-card').length,engine:document.querySelector('#engine').value,fingerprint:document.querySelector('#result-provenance').textContent})`));
  if (!resultCount) throw new Error('Search produced no results');
  await evaluate(`document.querySelector('#results-list .button-compare').click();document.querySelector('#candidate-view-tab').click();true`);
  await until(`!document.querySelector('#candidate-view').hidden&&document.querySelector('#candidate-player').readyState>=1`, 20);
  log('candidate_preview', await evaluate(`({visible:!document.querySelector('#candidate-view').hidden,duration:document.querySelector('#candidate-player').duration})`));
  await evaluate(`document.querySelector('#candidate-context').click();true`);
  const context = await evaluate(`document.querySelector('#active-range-label').textContent`);
  if (context !== '00:00 – 00:06') throw new Error(`Unexpected context range ${context}`);
  log('five_second_context_clamped_to_media', context);
  await evaluate(`document.querySelector('#candidate-context').click();true`);
  const playResult = await evaluate(`document.querySelector('#candidate-player').play().then(()=>({ok:true})).catch(e=>({ok:false,name:e.name,message:e.message}))`);
  await wait(700);
  const playback = await evaluate(`({paused:document.querySelector('#candidate-player').paused,time:document.querySelector('#candidate-player').currentTime,error:document.querySelector('#candidate-player').error?.code||null})`);
  if (!playResult.ok || playback.paused || playback.time <= 0 || playback.error) throw new Error(`Candidate did not play: ${JSON.stringify({ playResult, playback })}`);
  log('candidate_playback', playback);
  const cardCount = await evaluate(`document.querySelectorAll('#results-list .match-card').length`);
  await evaluate(`document.querySelector('#results-list .button-keep').click();true`);
  await until(`document.querySelector('#results-list .button-keep').disabled`, 10);
  if (cardCount < 2) throw new Error('Need at least two results to verify keep and hold separately');
  await evaluate(`document.querySelectorAll('#results-list .button-hold')[1].click();true`);
  await until(`document.querySelectorAll('#results-list .button-hold')[1].disabled`, 10);
  log('keep_hold_decisions', await evaluate(`Array.from(document.querySelectorAll('#references-list .decision-pill')).map(n=>n.textContent)`));
  await until(`document.querySelector('#export-references').getAttribute('aria-disabled')==='false'`, 10);
  await evaluate(`document.querySelector('#export-references').click();true`);
  let exported;
  for (let i = 0; i < 120; i++) {
    const targetFile = path.join(downloadDir, 'references.json');
    if (fs.existsSync(targetFile)) { exported = JSON.parse(fs.readFileSync(targetFile, 'utf8')); break; }
    await wait(150);
  }
  if (!exported || exported.length !== 2 || !exported.some(row => row.decision === 'keep') || !exported.some(row => row.decision === 'hold')) throw new Error('Export did not contain keep and hold references');
  log('references_export', { rows: exported.length, decisions: exported.map(row => row.decision) });
  const shot = await page.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  const png = Buffer.from(shot.data, 'base64');
  fs.writeFileSync(screenshotPath, png, { flag: 'wx' });
  log('synthetic_only_screenshot', { path: path.relative(root, screenshotPath), bytes: png.length });
  console.log(`QA_RESULT=PASS qa_id=${qaId}`);
} finally {
  page?.close();
  for (const child of [chromeProcess, serverProcess]) {
    if (child && child.exitCode === null) child.kill('SIGTERM');
  }
  await wait(400);
  fs.rmSync(chromeProfile, { recursive: true, force: true });
  fs.rmSync(tempRoot, { recursive: true, force: true });
}
