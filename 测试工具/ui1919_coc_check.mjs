// 1.9.19 CoC UI 回归：剧本下拉 / SAN 面板 / 商店 / 搜刮（Qt CDP）
// 用法：node 测试工具/ui1919_coc_check.mjs [cdpPort] [url]
const port = process.argv[2] || '9223';
const url = process.argv[3] || 'http://127.0.0.1:8765/';
const list = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
const page = list.find(t => t.type === 'page') || list[0];
if (!page) throw new Error('没有可用的 CDP page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
let seq = 0;
const errors = [];
await new Promise((res, rej) => {
  ws.addEventListener('open', res);
  ws.addEventListener('error', rej);
});
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id);
    pending.delete(m.id);
    m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    return;
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
  const r = await send('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 1200));
  return r.result.value;
}
async function waitFor(expr, timeout = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try {
      if (await ev(expr)) return true;
    } catch (e) { /* retry */ }
    await sleep(300);
  }
  return false;
}
await send('Runtime.enable');
await send('Page.enable');
await send('Log.enable');
await send('Page.navigate', { url });
await sleep(2200);
await ev('localStorage.clear()');
await send('Page.reload', { ignoreCache: true });
await sleep(2600);

await ev(`document.getElementById('mode-btn-coc').click()`);
await sleep(1200);
const setup = await ev(`(() => ({
  mode: S.mode,
  scenarioOptions: Array.from(document.getElementById('coc-scenario-preset').options)
    .map(o => ({ value: o.value, text: o.textContent })),
}))()`);

await ev(`document.getElementById('setup-next').click()`);
await sleep(500);
const setup2 = await ev(`(() => {
  const active = document.querySelector('.setup-step.active');
  const sel = document.getElementById('coc-scenario-preset');
  sel.value = 'manor';
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return {
    activeStep: active ? active.dataset.step : '',
    preset: collectCocScenarioId(),
    desc: document.getElementById('coc-scenario-desc').textContent,
    worldVisible: !document.getElementById('vn-anchor-box').classList.contains('hidden'),
  };
})()`);

await ev(`document.getElementById('inp-title').value = 'UI1919 CoC 验证';
  document.getElementById('btn-start').click()`);
const entered = await waitFor('!!S.sid && S.mode && S.mode.indexOf("coc") === 0', 30000);
await sleep(1200);
const game = await ev(`(() => ({
  sid: S.sid || '',
  mode: S.mode || '',
  title: S.title || '',
  san: (S.coc || {}).san,
  sanMax: (S.coc || {}).san_max,
  sanBoxHidden: document.getElementById('coc-san-box').classList.contains('hidden'),
  invCount: (((S.coc || {}).inventory || {}).counts || {}).backpack,
  shopBtn: !!document.getElementById('btn-coc-shop'),
  searchBtn: !!document.getElementById('btn-coc-search'),
  sanBtn: !!document.getElementById('btn-coc-san-check'),
  panelHidden: document.getElementById('coc-panel').classList.contains('hidden'),
  bg: (S.scene || ''),
}))()`);

await ev(`document.getElementById('btn-coc-shop').click()`);
await sleep(1200);
const shop = await ev(`(() => ({
  hidden: document.getElementById('modal-shop').classList.contains('hidden'),
  tabs: Array.from(document.querySelectorAll('#shop-tabs .shop-tab')).map(x => x.textContent),
  stock: (S.shop && S.shop.stock ? S.shop.stock.length : 0),
  scene: (S.shop && S.shop.scene ? S.shop.scene.slice(0, 40) : ''),
  funds: (S.shop || {}).funds,
  buyButtons: document.querySelectorAll('.shop-buy').length,
}))()`);
const beforeFunds = shop.funds || 0;
const buy = await ev(`(() => {
  const b = document.querySelector('.shop-buy:not([disabled])');
  if (!b) return { clicked: false };
  b.click();
  return { clicked: true, item: b.dataset.item };
})()`);
await sleep(900);
const afterBuy = await ev(`(() => ({
  funds: (S.shop || {}).funds,
  bag: (((S.coc || {}).inventory || {}).backpack || []).map(x => x.id),
  msg: document.getElementById('shop-msg').textContent,
}))()`);
await ev(`document.getElementById('btn-shop-close').click()`);

const beforeSan = await ev(`(S.coc || {}).san`);
await ev(`document.getElementById('btn-coc-san-check').click()`);
await sleep(900);
const afterSan = await ev(`(() => ({
  san: (S.coc || {}).san,
  checks: (S.coc || {}).san_checks,
  msg: document.getElementById('coc-san-msg').textContent,
  value: document.getElementById('coc-san-val').textContent,
}))()`);

await ev(`document.getElementById('btn-coc-search').click()`);
await sleep(700);
const search = await ev(`(() => ({
  searched: ((S.coc || {}).inventory || {}).counts,
  msg: document.getElementById('coc-loot-msg').textContent,
  sanMsg: document.getElementById('coc-san-msg').textContent,
}))()`);

const shot = await send('Page.captureScreenshot', { format: 'png' });
const fs = await import('node:fs');
fs.writeFileSync('测试样本/ui1919_coc.png', Buffer.from(shot.data, 'base64'));

console.log(JSON.stringify({
  setup, setup2, entered, game, shop, beforeFunds, buy, afterBuy,
  beforeSan, afterSan, search, errors,
}, null, 2));
ws.close();
