// 1.9.1 回归：选项拖动/点击 + 弹窗合成层（CDP）
import fs from 'node:fs';
const port = process.argv[2] || '9222';
const targetUrl = process.argv[3] || 'http://127.0.0.1:8799/';
const list = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
const page = list.find(t => t.type === 'page' && /about:blank|127\.0\.0\.1/.test(t.url)) || list.find(t => t.type === 'page');
if (!page) throw new Error('no page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map(); let seq = 0; const errors = [];
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return; }
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map(a => a.value || a.description || '').join(' '));
  if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') errors.push(m.params.entry.text);
});
function send(method, params = {}) { const id = ++seq; return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); }); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function ev(expression) { const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 500)); return r.result.value; }
async function mouse(type, x, y) { return send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 }); }

await send('Runtime.enable'); await send('Page.enable'); await send('Log.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 960, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: targetUrl }); await sleep(1800);
await ev('localStorage.clear()'); await send('Page.reload', { ignoreCache: true }); await sleep(2400);
await ev(`(() => { const s=(id,v)=>{const n=document.getElementById(id); if(!n)return; n.value=v; n.dispatchEvent(new Event('input',{bubbles:true})); n.dispatchEvent(new Event('change',{bubbles:true}));}; s('inp-title','1.9.1 选项回归'); s('inp-world','现代都市的学园故事，主角捡到一块怀表。'); s('prot-name','林澈'); document.getElementById('btn-start').click(); })()`);
await sleep(9000);
// 对话逐句播放：点对话框直到选项出现（最多 10 次）
for (let i = 0; i < 10; i++) {
  const n = await ev(`document.querySelectorAll('#choices-wrap .choice-btn').length`);
  if (n > 0) break;
  await ev(`document.getElementById('dialog-box').click()`);
  await sleep(320);
}
await sleep(500);

const before = await ev(`({
  active: document.querySelector('#game-screen').classList.contains('active'),
  choices: document.querySelectorAll('#choices-wrap .choice-btn').length,
  choiceHidden: document.getElementById('choices-wrap').classList.contains('hidden'),
  userSelect: getComputedStyle(document.querySelector('.choice-btn')||document.body).userSelect,
  modalFilter: getComputedStyle(document.querySelector('.modal')).backdropFilter,
  modalBg: getComputedStyle(document.querySelector('.modal')).backgroundColor,
  scene: (document.getElementById('scene-tag')||{}).textContent || '',
})`);

const rects = await ev(`Array.from(document.querySelectorAll('#choices-wrap .choice-btn')).map(b=>{const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};})`);
let hover = [];
for (let i = 0; i < rects.length; i++) {
  await mouse('mouseMoved', rects[i].x, rects[i].y);
  await sleep(120);
  hover.push(await ev(`({
    index: ${i},
    hot: document.querySelectorAll('#choices-wrap .choice-btn.hot').length,
    hotText: (document.querySelector('#choices-wrap .choice-btn.hot')||{}).textContent || '',
  })`));
}
let drag = null;
if (rects.length >= 2) {
  const a = rects[0], b = rects[rects.length - 1];
  await mouse('mousePressed', a.x, a.y);
  for (let i = 1; i <= 8; i++) {
    await mouse('mouseMoved', a.x + (b.x - a.x) * i / 8, a.y + (b.y - a.y) * i / 8);
    await sleep(15);
  }
  await mouse('mouseReleased', b.x, b.y);
  await sleep(250);
  drag = await ev(`({
    selection: String(window.getSelection()),
    dragging: document.getElementById('choices-wrap').classList.contains('dragging'),
    choices: document.querySelectorAll('#choices-wrap .choice-btn').length,
  })`);
}

// 正常点击第一个选项
const first = await ev(`(() => { const b=document.querySelector('#choices-wrap .choice-btn'); if(!b) return null; const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
let click = null;
if (first) {
  const t0 = Date.now();
  await mouse('mousePressed', first.x, first.y);
  await mouse('mouseReleased', first.x, first.y);
  await sleep(500);
  const immediate = await ev(`({ hidden: document.getElementById('choices-wrap').classList.contains('hidden'), loading: !document.getElementById('loading-overlay').classList.contains('hidden'), selection: String(window.getSelection()) })`);
  await sleep(4000);
  const after = await ev(`({ choices: document.querySelectorAll('#choices-wrap .choice-btn').length, hidden: document.getElementById('choices-wrap').classList.contains('hidden'), turn: (document.getElementById('scene-tag')||{}).textContent || '' })`);
  click = { immediate, after, ms: Date.now() - t0 };
}

console.log(JSON.stringify({ before, hover, drag, click, errors }, null, 2));
ws.close();
