// 1.8.0 UI smoke test over CDP (no external deps; Node >=22 global WebSocket)
//
// 用法（需先起隔离服务 + 一个开了 CDP 的浏览器）：
//   python server.py   # 另开终端；建议 TSF_PORT=8799 TSF_DATA_DIR=.devdata
//   node 测试工具/ui18_cdp_smoke.mjs <CDP端口> <截图目录> [页面URL]
// 例：node 测试工具/ui18_cdp_smoke.mjs 65098 .devdata/shots6 http://127.0.0.1:8799/
// CDP 端口可从浏览器 profile 的 DevToolsActivePort 读取（Chrome/Edge --remote-debugging-port=0）。
import fs from 'node:fs';
import path from 'node:path';

const port = process.argv[2] || '9222';
const base = `http://127.0.0.1:${port}`;
const outDir = process.argv[3] || '.devdata/shots';
const targetUrl = process.argv[4] || 'http://127.0.0.1:8799/';
fs.mkdirSync(outDir, { recursive: true });

const list = await fetch(`${base}/json/list`).then((r) => r.json());
const page = list.find((t) => t.type === 'page' && /about:blank|127\.0\.0\.1/.test(t.url)) || list.find((t) => t.type === 'page');
if (!page) throw new Error('no page target');
console.log('target:', page.id, page.url);

const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
const consoleErrors = [];
let seq = 0;

await new Promise((res, rej) => {
  ws.addEventListener('open', res);
  ws.addEventListener('error', rej);
});

ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
    return;
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    consoleErrors.push(msg.params.args.map((a) => a.value || a.description || '').join(' '));
  }
  if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') {
    consoleErrors.push(msg.params.entry.text);
  }
});

function send(method, params = {}) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function evaluate(expression) {
  const r = await send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (r.exceptionDetails) throw new Error('eval failed: ' + JSON.stringify(r.exceptionDetails).slice(0, 800));
  return r.result.value;
}
async function shot(name) {
  const r = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
  const p = path.join(outDir, name + '.png');
  fs.writeFileSync(p, Buffer.from(r.data, 'base64'));
  console.log('shot:', p);
}

await send('Runtime.enable');
await send('Page.enable');
await send('Log.enable');
await send('Log.clear');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: targetUrl });
await sleep(1600);
// 清掉上一次冒烟留下的会话，确保从「开始界面」开始验收
await evaluate('localStorage.clear()');
await send('Page.reload', { ignoreCache: true });
await sleep(2200);
await send('Log.clear');

const results = {};
results.title = await evaluate('document.title');
results.uiInit = await evaluate('document.documentElement.dataset.tsfUiInit || ""');
results.setupActive = await evaluate('document.querySelector("#setup-screen").classList.contains("active")');
results.step = await evaluate('(document.querySelector(".setup-nav-item.active")||{}).dataset?.step || ""');
results.ui2 = await evaluate('!!window.UI2');
results.railVersion = await evaluate('(document.getElementById("rail-version")||{}).textContent || ""');
results.ids = await evaluate(`(() => {
  const need = ['setup-nav','setup-main','stage-track','modal-stages','studio-grid','modal-perf','palette-list','btn-stage-studio'];
  return need.map(id => [id, !!document.getElementById(id)]);
})()`);
await shot('01_setup_story');

// wizard navigation
await evaluate('document.querySelector(".setup-nav-item[data-step=cast]").click()');
await sleep(350);
results.stepAfterCast = await evaluate('(document.querySelector(".setup-nav-item.active")||{}).dataset?.step || ""');
results.castVisible = await evaluate('!!document.querySelector(".setup-step[data-step=cast]").classList.contains("active")');
await shot('02_setup_cast');
await evaluate('document.querySelector(".setup-nav-item[data-step=files]").click()');
await sleep(300);
results.stepAfterFiles = await evaluate('(document.querySelector(".setup-nav-item.active")||{}).dataset?.step || ""');
await shot('03_setup_files');

// accessibility / duplicate id sanity
results.duplicateIds = await evaluate(`(() => {
  const ids = Array.from(document.querySelectorAll('[id]')).map(n => n.id);
  const seen = new Set(), dup = [];
  ids.forEach(i => { if (seen.has(i)) dup.push(i); seen.add(i); });
  return dup;
})()`);

// start a mock game through the real UI
await evaluate(`(() => {
  const set = (id, v) => { const n = document.getElementById(id); if (!n) return; n.value = v; n.dispatchEvent(new Event('input', {bubbles:true})); n.dispatchEvent(new Event('change', {bubbles:true})); };
  set('inp-title', 'UI2 冒烟测试');
  set('inp-world', '现代都市。主角在旧货市场淘到一块古董怀表，从此身体开始悄然改变。');
  set('prot-name', '林澈');
  document.getElementById('btn-start').click();
})()`);
await sleep(6000);
results.gameActive = await evaluate('document.querySelector("#game-screen").classList.contains("active")');
results.gameTitle = await evaluate('(document.getElementById("game-title")||{}).textContent || ""');
results.stageTrackPct = await evaluate('(document.getElementById("stage-track-pct")||{}).textContent || ""');
results.artStatus = await evaluate('(document.getElementById("art-status-text")||{}).textContent || ""');
results.menus = await evaluate('document.querySelectorAll(".top-group").length');
await shot('04_game_screen');

// topbar menu opens
await evaluate('document.querySelector(".top-group[data-menu=art] .top-trigger").click()');
await sleep(250);
results.artMenuOpen = await evaluate('document.querySelector(".top-group[data-menu=art]").classList.contains("open")');
results.artMenuVisible = await evaluate('getComputedStyle(document.querySelector(".top-group[data-menu=art] .top-menu")).display');
await shot('05_game_menu');

// command palette
await evaluate('document.getElementById("btn-palette").click()');
await sleep(300);
results.paletteOpen = await evaluate('!document.getElementById("modal-palette").classList.contains("hidden")');
results.paletteItems = await evaluate('document.querySelectorAll(".palette-item").length');
await shot('06_palette');
await evaluate('document.getElementById("palette-input").value = "六段"; document.getElementById("palette-input").dispatchEvent(new Event("input",{bubbles:true}));');
await sleep(250);
results.paletteFiltered = await evaluate('document.querySelectorAll(".palette-item").length');
await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');

// stage studio
await evaluate('document.getElementById("btn-stage-studio").click()');
await sleep(1500);
results.studioOpen = await evaluate('!document.getElementById("modal-stages").classList.contains("hidden")');
results.studioCards = await evaluate('document.querySelectorAll("#studio-grid .stage-card").length');
results.studioPct = await evaluate('(document.getElementById("studio-pct")||{}).textContent || ""');
await shot('07_studio_before');

// run all six stages (mock generation)
await evaluate('document.getElementById("btn-stages-run").click()');
await sleep(14000);
results.studioAfter = await evaluate(`(() => {
  const cards = Array.from(document.querySelectorAll('#studio-grid .stage-card'));
  return {
    ready: cards.filter(c => c.classList.contains('ready')).length,
    pending: cards.filter(c => c.classList.contains('pending')).length,
    eta: (document.getElementById('studio-eta')||{}).textContent || '',
    bar: (document.getElementById('studio-bar')||{}).style?.width || ''
  };
})()`);
results.studioLayout = await evaluate(`(() => {
  const card = document.querySelector('#studio-grid .stage-card');
  const thumb = card && card.querySelector('.stage-thumb');
  const body = card && card.querySelector('.stage-body');
  const grid = document.getElementById('studio-grid');
  return { card: card && card.clientHeight, thumb: thumb && thumb.clientHeight,
    body: body && body.clientHeight, grid: grid && [grid.clientHeight, grid.scrollHeight],
    rows: getComputedStyle(grid).gridTemplateRows };
})()`);
await shot('08_studio_after');

// perf panel
await evaluate('document.getElementById("btn-studio-perf").click()');
await sleep(1200);
results.perfTiles = await evaluate('document.querySelectorAll("#perf-grid .perf-tile").length');
results.perfSD = await evaluate('(document.getElementById("perf-sd")||{}).textContent || ""');
await shot('09_perf');

results.consoleErrors = consoleErrors;
console.log(JSON.stringify(results, null, 2));
ws.close();
