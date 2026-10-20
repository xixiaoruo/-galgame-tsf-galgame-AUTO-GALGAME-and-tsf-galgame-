// 1.9.19 开局剧本下拉视觉核对（不开始游戏）
const port = process.argv[2] || '9223';
const url = process.argv[3] || 'http://127.0.0.1:8765/';
const list = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
const page = list.find(t => t.type === 'page') || list[0];
const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map(); let seq = 0;
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
});
function send(method, params = {}) { const id = ++seq; return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); }); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function ev(expression) { const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 800)); return r.result.value; }
await send('Runtime.enable'); await send('Page.enable');
await send('Page.navigate', { url }); await sleep(1800);
await ev('localStorage.clear()'); await send('Page.reload', { ignoreCache: true }); await sleep(2200);
await ev(`document.getElementById('mode-btn-coc').click()`);
await sleep(1200);
await ev(`document.getElementById('setup-next').click()`);
await sleep(800);
const state = await ev(`(() => {
  const sel = document.getElementById('coc-scenario-preset');
  return {
    active: document.querySelector('.setup-step.active').dataset.step,
    options: Array.from(sel.options).map(o => ({ value: o.value, text: o.textContent })),
    desc: document.getElementById('coc-scenario-desc').textContent,
  };
})()`);
const shot = await send('Page.captureScreenshot', { format: 'png' });
const fs = await import('node:fs');
fs.writeFileSync('测试样本/ui1919_setup.png', Buffer.from(shot.data, 'base64'));
console.log(JSON.stringify(state, null, 2));
ws.close();
