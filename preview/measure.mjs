// 精确几何：区分「真遮挡」与「滚动容器裁剪造成的假象」
import pw from 'file:///Users/xu/.workbuddy/binaries/node/workspace/node_modules/playwright/index.js';
const { chromium } = pw;
import fs from 'node:fs';

const BASE = 'http://localhost:8765';
const PLAN_ID = process.env.PLAN_ID;
const TOKEN = process.env.PROBE_TOKEN;

const VPS = [
  { name: 'desktop-1440', w: 1440, h: 900, mobile: false },
  { name: 'mobile-390', w: 390, h: 844, mobile: true },
  { name: 'mobile-320', w: 320, h: 568, mobile: true },
  { name: 'mobile-landscape', w: 844, h: 390, mobile: true },
];

const geo = () => {
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { t: Math.round(r.top), b: Math.round(r.bottom), l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height) };
  };
  const clipOf = (el) => {
    // 逐层向上取所有 overflow 非 visible 的祖先裁剪框交集
    let box = { t: 0, b: innerHeight, l: 0, r: innerWidth };
    let n = el.parentElement;
    while (n) {
      const cs = getComputedStyle(n);
      if (/(hidden|auto|scroll|clip)/.test(cs.overflowX + cs.overflowY)) {
        const r = n.getBoundingClientRect();
        box = { t: Math.max(box.t, r.top), b: Math.min(box.b, r.bottom), l: Math.max(box.l, r.left), r: Math.min(box.r, r.right) };
      }
      n = n.parentElement;
    }
    return box;
  };
  const occl = [];
  for (const el of document.querySelectorAll('button, a, [role="button"], input, textarea, .rd-stop')) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.pointerEvents === 'none') continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const clip = clipOf(el);
    // 中心点若被祖先裁剪掉，说明本来就没露出来 —— 不算遮挡
    if (cx < clip.l + 1 || cx > clip.r - 1 || cy < clip.t + 1 || cy > clip.b - 1) continue;
    if (cy < 0 || cy > innerHeight || cx < 0 || cx > innerWidth) continue;
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || hit === el || el.contains(hit) || hit.contains(el)) continue;
    if (getComputedStyle(hit).pointerEvents === 'none') continue;
    occl.push({
      target: ((el.className || el.tagName) + '').slice(0, 40) + ' :: ' + (el.textContent || '').trim().slice(0, 24),
      blocker: ((hit.className || hit.tagName) + '').slice(0, 40) + ' :: ' + (hit.textContent || '').trim().slice(0, 24),
      center: [Math.round(cx), Math.round(cy)],
    });
  }
  const q = (s) => rect(document.querySelector(s));
  return {
    vh: innerHeight, vw: innerWidth,
    header: q('.topbar'),
    dock: q('.bottom-dock'),
    dashboardPad: (() => {
      const d = document.querySelector('.dashboard-page');
      if (!d) return null;
      const cs = getComputedStyle(d);
      return { padBottom: cs.paddingBottom, h: Math.round(d.getBoundingClientRect().height) };
    })(),
    routeCol: q('.rd-route-column'),
    timeline: q('.rd-timeline'),
    detailPanel: q('.rd-detail-panel'),
    tabs: q('.mobile-detail-tabs'),
    fab: q('.rd-fab, .chat-fab, [class*="fab"]'),
    occl,
  };
};

const browser = await chromium.launch();
const res = {};
for (const vp of VPS) {
  const ctx = await browser.newContext({
    viewport: { width: vp.w, height: vp.h }, isMobile: vp.mobile, hasTouch: vp.mobile, deviceScaleFactor: vp.mobile ? 2 : 1,
    userAgent: vp.mobile ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1' : undefined,
  });
  await ctx.addInitScript(([t]) => { if (t) localStorage.setItem('auth', JSON.stringify({ token: t, username: 'probe_ui_01' })); }, [TOKEN]);
  const page = await ctx.newPage();
  const r = {};
  await page.goto(BASE + '/', { waitUntil: 'networkidle' }); await page.waitForTimeout(500);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await page.waitForTimeout(300);
  r.home = await page.evaluate(geo);
  await page.goto(BASE + `/?view_plan_id=${PLAN_ID}`, { waitUntil: 'networkidle' }); await page.waitForTimeout(1500);
  r.detail = await page.evaluate(geo);
  // 滚到列表底部后再测一次
  await page.evaluate(() => { const c = document.querySelector('.rd-route-column'); if (c) c.scrollTop = c.scrollHeight; });
  await page.waitForTimeout(400);
  r.detailEnd = await page.evaluate(geo);
  res[vp.name] = r;
  await page.close(); await ctx.close();
}
await browser.close();
fs.writeFileSync('preview/verify/geometry.json', JSON.stringify(res, null, 2));
for (const [k, v] of Object.entries(res)) {
  console.log(`\n### ${k} vh=${v.home.vh}`);
  console.log('  home dock', JSON.stringify(v.home.dock), 'dashPad', JSON.stringify(v.home.dashboardPad));
  console.log('  home occl', JSON.stringify(v.home.occl));
  console.log('  detail routeCol', JSON.stringify(v.detail.routeCol), 'tabs', JSON.stringify(v.detail.tabs));
  console.log('  detail fab', JSON.stringify(v.detail.fab));
  console.log('  detail occl', JSON.stringify(v.detail.occl));
  console.log('  detailEnd occl', JSON.stringify(v.detailEnd.occl));
}
