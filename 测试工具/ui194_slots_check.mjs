// 1.9.4 存档/读档回归（真实 QtWebEngine CDP）
import fs from 'node:fs';
const port = process.argv[2] || '9222';
const targetUrl = process.argv[3] || 'http://127.0.0.1:8797/';
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
async function ev(expression) { const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 800)); return r.result.value; }

await send('Runtime.enable'); await send('Page.enable'); await send('Log.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 960, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: targetUrl }); await sleep(1800);
await ev('localStorage.clear()'); await send('Page.reload', { ignoreCache: true }); await sleep(2400);

// 开局 A
await ev(`(() => { const s=(id,v)=>{const n=document.getElementById(id); if(!n)return; n.value=v; n.dispatchEvent(new Event('input',{bubbles:true})); n.dispatchEvent(new Event('change',{bubbles:true}));}; s('inp-title','存档回归A'); s('inp-world','现代都市学园故事，A 局。'); s('prot-name','林澈'); document.getElementById('btn-start').click(); })()`);
await sleep(9000);
for (let i = 0; i < 10; i++) {
  if (await ev(`document.querySelectorAll('#choices-wrap .choice-btn').length`) > 0) break;
  await ev(`document.getElementById('dialog-box').click()`); await sleep(250);
}
const sidA = await ev('S.sid');
const turnA = await ev('S.turn && S.turn.turn_no');

// 通过 UI 存档 A
await ev('document.getElementById("btn-save").click()');
await sleep(700);
await ev(`(() => { const n=document.getElementById('slot-name'); n.value='回归档A'; n.dispatchEvent(new Event('input',{bubbles:true})); document.getElementById('btn-slot-save').click(); })()`);
await sleep(1200);
const listAfterSave = await ev(`Array.from(document.querySelectorAll('#slot-list .slot-row')).map(r=>r.textContent.trim())`);

// 创建 B 局并直接存入 global slots
const bState = await ev(`(async () => {
  const st = await api('/api/game/start', {method:'POST', body: JSON.stringify({
    mode:'tsf', title:'存档回归B', world:'现代都市学园故事，B 局。',
    protagonist:{name:'顾言', anchor:'黑色短发，深色眼瞳，日常便装'},
    characters:[], content_rating:'all', r18_enabled:false,
  })});
  const saved = await api('/api/game/' + st.sid + '/slots', {method:'POST', body: JSON.stringify({name:'回归档B'})});
  return {sid: st.sid, slot: saved.slot_id};
})()`);
await sleep(600);

// 刷新存档列表，读取 B 档
await ev('document.getElementById("btn-load").click()');
await sleep(900);
const rows = await ev(`Array.from(document.querySelectorAll('#slot-list .slot-row')).map(r=>r.textContent.trim())`);
const before = await ev(`({sid:S.sid, stored:localStorage.getItem('galgame_sid')})`);
await ev(`(() => {
  const rows = Array.from(document.querySelectorAll('#slot-list .slot-row'));
  const target = rows.find(r => r.textContent.includes('回归档B'));
  if (!target) throw new Error('B slot row not found: ' + rows.map(x=>x.textContent).join(' | '));
  target.querySelector('.slot-load').click();
})()`);
await sleep(1800);
const afterLoad = await ev(`({sid:S.sid, stored:localStorage.getItem('galgame_sid'), title:S.title, turn:S.turn && S.turn.turn_no})`);

// 读档后推进一幕：必须仍发到 B
const beforeTurn = afterLoad.turn;
await ev('choose(0)');
await sleep(5000);
const afterAdvance = await ev(`({sid:S.sid, stored:localStorage.getItem('galgame_sid'), title:S.title, turn:S.turn && S.turn.turn_no})`);

// 设置里的原生 select：change 后应 blur，不留焦点外环
await ev('document.getElementById("btn-settings-game").click()');
await sleep(500);
const selectState = await ev(`(() => {
  const s = document.getElementById('cfg-portrait-quality');
  s.focus(); s.value='low'; s.dispatchEvent(new Event('change',{bubbles:true}));
  return {active: document.activeElement && document.activeElement.id, shadow: getComputedStyle(s).boxShadow};
})()`);
await ev('document.getElementById("btn-settings-close").click()');

console.log(JSON.stringify({
  sidA, turnA, listAfterSave, bState, rows, before, afterLoad, beforeTurn,
  afterAdvance, selectState, errors,
}, null, 2));
ws.close();
