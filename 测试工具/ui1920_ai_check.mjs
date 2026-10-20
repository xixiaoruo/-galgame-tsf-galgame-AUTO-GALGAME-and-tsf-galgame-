// 1.9.20 AI 控制台 UI 回归：打开控制台 / 查看请求 / 改对话 / 保存导演指令
const port = process.argv[2] || '9223';
const url = process.argv[3] || 'http://127.0.0.1:8765/';
const list = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
const page = list.find(t => t.type === 'page') || list[0];
const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map(); let seq = 0; const errors = [];
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id); pending.delete(m.id);
    m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return;
  }
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
    errors.push(m.params.args.map(a => a.value || a.description || '').join(' '));
  }
  if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
    errors.push(m.params.entry.text);
  }
});
function send(method, params = {}) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function ev(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 1200));
  return r.result.value;
}
async function waitFor(expr, timeout = 25000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { if (await ev(expr)) return true; } catch (e) { /* retry */ }
    await sleep(300);
  }
  return false;
}
await send('Runtime.enable'); await send('Page.enable'); await send('Log.enable');
await send('Page.navigate', { url }); await sleep(2200);
await ev('localStorage.clear()'); await send('Page.reload', { ignoreCache: true }); await sleep(2600);
await ev(`document.getElementById('mode-btn-coc').click()`); await sleep(1000);
await ev(`document.getElementById('setup-next').click()`); await sleep(400);
await ev(`(() => { const s=document.getElementById('coc-scenario-preset'); s.value='manor'; s.dispatchEvent(new Event('change',{bubbles:true})); })()`);
await ev(`document.getElementById('inp-title').value='AI控制台UI验证'; document.getElementById('btn-start').click()`);
await waitFor(`!!S.sid && (S.mode||'').indexOf('coc')===0`, 30000);
await sleep(800);

const before = await ev(`(() => ({
  sid: S.sid, mode: S.mode,
  dialogue: (S.turn && S.turn.dialogue || []).map(x=>x.text),
  btn: !!document.getElementById('btn-ai-console'),
}))()`);
await ev(`document.getElementById('btn-ai-console').click()`);
await sleep(1000);
const opened = await ev(`(() => ({
  hidden: document.getElementById('modal-ai').classList.contains('hidden'),
  entries: (S.aiData && S.aiData.entries || []).length,
  tabs: Array.from(document.querySelectorAll('.ai-tabs button')).map(x=>x.textContent),
  detail: (document.getElementById('ai-entry-detail').textContent || '').slice(0, 120),
}))()`);
await ev(`document.getElementById('ai-tab-edit').click()`);
await sleep(300);
const editRows = await ev(`document.querySelectorAll('#ai-dialogue-editor .ai-dialogue-row').length`);
const editedText = '（UI 测试改写的旁白）';
await ev(`(() => {
  const t = document.querySelector('#ai-dialogue-editor .ai-text');
  if (!t) return false;
  t.value = ${JSON.stringify(editedText)};
  return true;
})()`);
await ev(`document.getElementById('btn-ai-save-dialogue').click()`);
await sleep(1200);
const saved = await ev(`(() => ({
  dialogue: (S.turn && S.turn.dialogue || []).map(x=>x.text),
  dom: document.getElementById('dialog-text').textContent,
  msg: document.getElementById('ai-edit-msg').textContent,
  entries: (S.aiData && S.aiData.entries || []).length,
}))()`);

await ev(`document.getElementById('ai-tab-direct').click()`);
await sleep(250);
await ev(`(() => {
  const t = document.getElementById('ai-directive');
  t.value = 'UI 测试导演指令：保持冷峻';
  document.getElementById('btn-ai-save-directive').click();
})()`);
await sleep(700);
const direct = await ev(`(() => ({
  value: document.getElementById('ai-directive').value,
  msg: document.getElementById('ai-direct-msg').textContent,
  state: (S.turn && S.turn.ai_directive) || S.aiDirective || '',
}))()`);

const shot = await send('Page.captureScreenshot', { format: 'png' });
const fs = await import('node:fs');
fs.writeFileSync('测试样本/ui1920_ai_console.png', Buffer.from(shot.data, 'base64'));
console.log(JSON.stringify({ before, opened, editRows, saved, direct, errors }, null, 2));
ws.close();
