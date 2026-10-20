// 打开商店并截图（仅用于 1.9.19 视觉核对）
const port = process.argv[2] || '9223';
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
const r = await send('Runtime.evaluate', { expression: `document.getElementById('btn-coc-shop').click()`, userGesture: true });
await new Promise(r => setTimeout(r, 1200));
const shot = await send('Page.captureScreenshot', { format: 'png' });
const fs = await import('node:fs');
fs.writeFileSync('测试样本/ui1919_shop.png', Buffer.from(shot.data, 'base64'));
console.log(JSON.stringify({ ok: !r.exceptionDetails, hidden: false }, null, 2));
ws.close();
