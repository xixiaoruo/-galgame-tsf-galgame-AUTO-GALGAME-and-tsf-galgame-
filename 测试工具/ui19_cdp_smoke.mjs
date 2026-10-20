// 1.9.0 UI smoke test over CDP (no external deps; Node >=22 global WebSocket)
// 用法：先起隔离服务 + 一个开了 CDP 的浏览器：
//   set TSF_PORT=8799 & set TSF_DATA_DIR=.devdata/ui190 & python server.py
//   node 测试工具/ui19_cdp_smoke.mjs <CDP端口> <截图目录> [页面URL]
import fs from 'node:fs';
import path from 'node:path';

const port = process.argv[2] || '9222';
const base = `http://127.0.0.1:${port}`;
const outDir = process.argv[3] || '.devdata/shots19';
const targetUrl = process.argv[4] || 'http://127.0.0.1:8799/';
fs.mkdirSync(outDir, { recursive: true });

const list = await fetch(`${base}/json/list`).then((r) => r.json());
const page = list.find((t) => t.type === 'page' && /about:blank|127\.0\.0\.1/.test(t.url))
  || list.find((t) => t.type === 'page');
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
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  });
  if (r.exceptionDetails) {
    throw new Error('eval failed: ' + JSON.stringify(r.exceptionDetails).slice(0, 900));
  }
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
await send('Emulation.setDeviceMetricsOverride', {
  width: 1440, height: 960, deviceScaleFactor: 1, mobile: false,
});
await send('Page.navigate', { url: targetUrl });
await sleep(1800);
await evaluate('localStorage.clear()');
await send('Page.reload', { ignoreCache: true });
await sleep(2400);
await send('Log.clear');
consoleErrors.length = 0;   // 清掉首次导航留下的旧错误，只统计本次 UI 操作

const results = {};
results.title = await evaluate('document.title');
results.uiInit = await evaluate('document.documentElement.dataset.tsfUiInit || ""');
results.version = await evaluate('(document.getElementById("rail-version")||{}).textContent || ""');
results.newIds = await evaluate(`(() => {
  const need = ['uniform-en','uniform-neg','uniform-name','uniform-preset-select',
    'btn-uniform-random','btn-legacy','modal-legacy','legacy-list',
    'btn-outfit-random','tsf-bond','vn-bond','btn-libpick-adapt'];
  return need.map(id => [id, !!document.getElementById(id)]);
})()`);

// 进入登场角色步骤
await evaluate('document.querySelector(".setup-nav-item[data-step=cast]").click()');
await sleep(350);

// 随机制服
await evaluate('document.getElementById("btn-uniform-random").click()');
await sleep(700);
results.uniform = await evaluate(`({
  en: (document.getElementById('uniform-en')||{}).value || '',
  neg: (document.getElementById('uniform-neg')||{}).value || '',
  name: (document.getElementById('uniform-name')||{}).textContent || '',
  selected: (document.getElementById('uniform-preset-select')||{}).value || ''
})`);
await shot('01_setup_cast_uniform');

// 角色卡：随机 + AI 背景
await evaluate('document.querySelector(".char-card .btn-char-random").click()');
await sleep(800);
results.randomChar = await evaluate(`({
  name: (document.querySelector('.char-card .char-name')||{}).value || '',
  role: (document.querySelector('.char-card .char-role')||{}).value || '',
  appearance: (document.querySelector('.char-card .char-appearance')||{}).value || ''
})`);
await evaluate('document.querySelector(".char-card .btn-char-ai").click()');
await sleep(900);
results.charBackground = await evaluate(`({
  text: (document.querySelector('.char-card .char-background')||{}).value || '',
  status: (document.querySelector('.char-card .char-bg-status')||{}).textContent || ''
})`);
await shot('02_setup_char_card');

// 角色库弹窗（含旧卡适配按钮 + 背景标记）
await evaluate('document.querySelector(".char-card .btn-char-lib").click()');
await sleep(700);
results.libPick = await evaluate(`({
  open: !document.getElementById('modal-libpick').classList.contains('hidden'),
  count: document.querySelectorAll('#libpick-list .libpick-item').length,
  adaptBtn: !!document.getElementById('btn-libpick-adapt'),
  withBg: Array.from(document.querySelectorAll('#libpick-list .libpick-item'))
    .filter(x => /有背景|背景待谱写/.test(x.textContent)).length
})`);
await shot('03_lib_picker');
await evaluate('document.getElementById("btn-libpick-close").click()');

// 旧档案找回弹窗
results.legacy = await evaluate(`(async () => {
  await openLegacy();
  await new Promise(r => setTimeout(r, 500));
  return {
    open: !document.getElementById('modal-legacy').classList.contains('hidden'),
    summary: (document.getElementById('legacy-summary')||{}).textContent || '',
    rows: document.querySelectorAll('#legacy-list .legacy-item').length,
    scan: (legacyScanData && legacyScanData.counts) || null,
  };
})()`);
await shot('04_legacy');
await evaluate('document.getElementById("btn-legacy-close").click()');

// 开局（mock LLM / mock 生图）
await evaluate(`(() => {
  const set = (id, v) => { const n = document.getElementById(id); if (!n) return;
    n.value = v; n.dispatchEvent(new Event('input', {bubbles:true}));
    n.dispatchEvent(new Event('change', {bubbles:true})); };
  set('inp-title', '1.9 冒烟测试');
  set('inp-world', '现代都市的星冠学园。主角在旧货市场淘到一块古董怀表，身体开始悄然改变。');
  set('prot-name', '林澈');
  document.getElementById('btn-start').click();
})()`);
await sleep(7500);
results.game = await evaluate(`({
  active: document.querySelector('#game-screen').classList.contains('active'),
  title: (document.getElementById('game-title')||{}).textContent || '',
  bond: (document.getElementById('tsf-bond')||{}).textContent || '',
  bondHidden: document.getElementById('tsf-bond').classList.contains('hidden'),
  charStats: (typeof S !== 'undefined' && S.charStats) ? {
    count: (S.charStats.characters||[]).length,
    first: (S.charStats.characters||[])[0] ? {
      name: S.charStats.characters[0].name,
      bond: S.charStats.characters[0].bond,
      note: !!S.charStats.characters[0].emotion_note,
    } : null,
  } : null,
})`);
await shot('05_game_bond');

// 换装：随机制服
await evaluate('document.getElementById("btn-outfit").click()');
await sleep(300);
await evaluate('document.getElementById("btn-outfit-random").click()');
await sleep(700);
results.outfitRandom = await evaluate(`({
  value: (document.getElementById('outfit-preset')||{}).value || '',
  note: (document.getElementById('outfit-random-note')||{}).textContent || '',
  hint: (document.getElementById('outfit-hint')||{}).textContent || '',
})`);
await shot('06_outfit_random');
await evaluate('document.getElementById("btn-outfit-close").click()');

results.consoleErrors = consoleErrors;
console.log(JSON.stringify(results, null, 2));
ws.close();
