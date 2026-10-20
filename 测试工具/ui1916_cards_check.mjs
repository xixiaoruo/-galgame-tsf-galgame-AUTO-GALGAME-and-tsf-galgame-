// 1.9.16 角色卡详细人设 / 固定属性 / AI 微调 UI 回归（Qt CDP）
const port = process.argv[2] || '9223';
const url = process.argv[3] || 'http://127.0.0.1:8797/';
const list = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
const page = list.find(t => t.type === 'page' && /about:blank|127\.0\.0\.1/.test(t.url)) || list.find(t => t.type === 'page');
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
async function ev(expression) { const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 1000)); return r.result.value; }
await send('Runtime.enable'); await send('Page.enable'); await send('Log.enable');
await send('Page.navigate', { url }); await sleep(1800);
await ev('localStorage.clear()'); await send('Page.reload', { ignoreCache: true }); await sleep(2200);
await ev(`document.getElementById('btn-add-char').click()`);
await sleep(200);
const fields = await ev(`(() => {
  const c = document.querySelector('.char-card');
  const set = (sel, v) => { const el = c.querySelector(sel); el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); };
  set('.char-name','测试角色'); set('.char-role','同班同学'); set('.char-appearance','黑色短发，深色眼瞳，日常便装');
  set('.char-personality','慢热、嘴硬心软'); set('.char-traits','温柔、慢热'); set('.char-age','22');
  set('.char-height','164'); set('.char-body','纤细高挑'); set('.char-measure','B86 / W58 / H88');
  set('.char-birthday','5月12日'); set('.char-speech','语速偏慢，措辞礼貌'); set('.char-likes','旧书'); set('.char-dislikes','吵闹');
  c.querySelector('.char-lock-personality').click();
  return {
    card: !!c, fields: ['.char-traits','.char-age','.char-height','.char-body','.char-measure','.char-birthday','.char-speech','.char-likes','.char-dislikes'].map(s=>!!c.querySelector(s)),
    locked: c.querySelector('.char-lock-personality').checked,
  };
})()`);
const before = await ev('collectNPCs()[0]');
await ev('openFineTune(document.querySelector(".char-card"))');
await sleep(200);
const modal = await ev(`({hidden: document.getElementById('modal-finetune').classList.contains('hidden'), target: document.getElementById('finetune-target').textContent})`);
await ev(`document.getElementById('finetune-note').value='身高改成170cm，体型更纤细，性格更活泼'; applyFineTune()`);
await sleep(5000);
const after = await ev('collectNPCs()[0]');
const hint = await ev(`document.getElementById('finetune-hint').textContent`);
console.log(JSON.stringify({ fields, before, modal, after, hint, errors }, null, 2));
ws.close();
