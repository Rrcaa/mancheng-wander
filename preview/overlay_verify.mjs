// 补测：登录弹窗 / 抽屉 / 侧栏，在小屏上的溢出与关闭路径
import pw from 'file:///Users/xu/.workbuddy/binaries/node/workspace/node_modules/playwright/index.js';
const { chromium } = pw;
import fs from 'node:fs';

const BASE = 'http://localhost:8765';
const PLAN_ID = process.env.PLAN_ID;
const TOKEN = process.env.PROBE_TOKEN;
const OUT = 'preview/verify';
fs.mkdirSync(OUT, { recursive: true });

const VPS = [
  { name: 'desktop-1440', w: 1440, h: 900, mobile: false },
  { name: 'mobile-390', w: 390, h: 844, mobile: true },
  { name: 'mobile-320', w: 320, h: 568, mobile: true },
  { name: 'mobile-landscape', w: 844, h: 390, mobile: true },
];

const probe = () => {
  const vw = innerWidth, vh = innerHeight;
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if ((r.right > vw + 2 || r.left < -2) && !/(auto|scroll)/.test(cs.overflowX)) {
      let n = el.parentElement, clipped = false;
      while (n) { const c = getComputedStyle(n); if (/(hidden|auto|scroll|clip)/.test(c.overflowX)) { const nr = n.getBoundingClientRect(); if (r.right > nr.right + 2 || r.left < nr.left - 2) { clipped = true; break; } } n = n.parentElement; }
      if (!clipped) bad.push({ sel: (el.className || el.tagName).toString().slice(0, 36), l: Math.round(r.left), r: Math.round(r.right) });
    }
  }
  const modal = document.querySelector('.modal-card, .auth-modal, [role="dialog"]');
  const mr = modal ? modal.getBoundingClientRect() : null;
  return {
    vw, vh,
    modalBox: mr ? { t: Math.round(mr.top), b: Math.round(mr.bottom), l: Math.round(mr.left), r: Math.round(mr.right), h: Math.round(mr.height) } : null,
    modalFitsV: mr ? mr.top >= -1 && mr.bottom <= vh + 1 : null,
    overflow: bad.slice(0, 8),
    docOverflow: document.documentElement.scrollWidth - vw,
  };
};

const browser = await chromium.launch();
for (const vp of VPS) {
  const ctx = await browser.newContext({
    viewport: { width: vp.w, height: vp.h }, isMobile: vp.mobile, hasTouch: vp.mobile, deviceScaleFactor: vp.mobile ? 2 : 1,
    userAgent: vp.mobile ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1' : undefined,
  });
  // 登录弹窗：不带 auth，用 ?login=1 触发
  const p1 = await ctx.newPage();
  await p1.goto(BASE + '/?login=1', { waitUntil: 'networkidle' });
  await p1.waitForTimeout(800);
  const loginState = await p1.evaluate(probe);
  await p1.screenshot({ path: `${OUT}/${vp.name}__login-modal.png` });
  console.log(`\n### ${vp.name} login`, JSON.stringify(loginState));
  await p1.close();

  // 抽屉：带 auth 打开详情，点「旅行资料」
  const ctx2 = await browser.newContext({
    viewport: { width: vp.w, height: vp.h }, isMobile: vp.mobile, hasTouch: vp.mobile, deviceScaleFactor: vp.mobile ? 2 : 1,
    userAgent: vp.mobile ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1' : undefined,
  });
  await ctx2.addInitScript(([t]) => { if (t) localStorage.setItem('auth', JSON.stringify({ token: t, username: 'probe_ui_01' })); }, [TOKEN]);
  const p2 = await ctx2.newPage();
  await p2.goto(BASE + `/?view_plan_id=${PLAN_ID}`, { waitUntil: 'networkidle' });
  await p2.waitForTimeout(1500);
  const clicked = await p2.evaluate(() => {
    const b = [...document.querySelectorAll('button')].find((x) => (x.textContent || '').trim() === '旅行资料');
    if (b) { b.click(); return true; }
    return false;
  });
  await p2.waitForTimeout(900);
  const drawerState = await p2.evaluate(() => {
    const d = document.querySelector('.sl-drawer:not([hidden])');
    if (!d) return { found: false };
    const r = d.getBoundingClientRect();
    return { found: true, t: Math.round(r.top), b: Math.round(r.bottom), h: Math.round(r.height), fits: r.top >= -1 && r.bottom <= innerHeight + 1, vh: innerHeight };
  });
  await p2.screenshot({ path: `${OUT}/${vp.name}__drawer.png` });
  console.log(`### ${vp.name} drawer clicked=${clicked}`, JSON.stringify(drawerState));
  await p2.close(); await ctx2.close(); await ctx.close();
}
await browser.close();
