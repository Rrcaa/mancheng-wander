// UI 深检：多视口 × 多页面，抓布局溢出 / 文字截断 / 触控尺寸 / 控制台错误 / 资源 404
// 用法：NODE_PATH=<workspace>/node_modules node preview/ui_verify.mjs
import pw from 'file:///Users/xu/.workbuddy/binaries/node/workspace/node_modules/playwright/index.js';
const { chromium } = pw;
import fs from 'node:fs';
import path from 'node:path';

const BASE = 'http://localhost:8765';
const PLAN_ID = process.env.PLAN_ID || 'be69953a13be4f6d9aed792d0c83313c';
const TOKEN = process.env.PROBE_TOKEN || '';
const USER = process.env.PROBE_USER || 'probe_ui_01';
const OUT = path.resolve('preview/verify');
fs.mkdirSync(OUT, { recursive: true });

const VIEWPORTS = [
  { name: 'desktop-1920', w: 1920, h: 1080, mobile: false },
  { name: 'desktop-1440', w: 1440, h: 900, mobile: false },
  { name: 'laptop-1280', w: 1280, h: 800, mobile: false },
  { name: 'tablet-834', w: 834, h: 1194, mobile: false, touch: true },
  { name: 'mobile-390', w: 390, h: 844, mobile: true },
  { name: 'mobile-375', w: 375, h: 667, mobile: true },
  { name: 'mobile-360', w: 360, h: 740, mobile: true },
  { name: 'mobile-320', w: 320, h: 568, mobile: true },
];

const PAGES = [
  { key: 'home', url: '/' },
  { key: 'discover', url: '/?page=discover' },
  { key: 'mine', url: '/?page=mine' },
  { key: 'chat', url: '/?page=chat' },
  { key: 'detail', url: `/?view_plan_id=${PLAN_ID}` },
];

const probe = () => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const docW = document.documentElement.scrollWidth;
  const cssPath = (el) => {
    const parts = [];
    let n = el;
    for (let i = 0; i < 4 && n && n.nodeType === 1; i++) {
      let s = n.tagName.toLowerCase();
      if (n.id) { s += '#' + n.id; parts.unshift(s); break; }
      if (n.className && typeof n.className === 'string') {
        const cls = n.className.trim().split(/\s+/).slice(0, 2).join('.');
        if (cls) s += '.' + cls;
      }
      parts.unshift(s);
      n = n.parentElement;
    }
    return parts.join(' > ');
  };
  const inScrollBox = (el) => {
    let n = el.parentElement;
    while (n && n !== document.body) {
      const cs = getComputedStyle(n);
      if (/(auto|scroll)/.test(cs.overflowX) || /(auto|scroll)/.test(cs.overflowY)) return true;
      n = n.parentElement;
    }
    return false;
  };

  const overflow = [];
  const clipped = [];
  const tiny = [];
  const smallTargets = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;

    // 横向溢出（排除内部滚动容器）
    if ((r.right > vw + 1.5 || r.left < -1.5) && !inScrollBox(el)) {
      const amt = Math.max(r.right - vw, -r.left);
      if (amt > 1.5) overflow.push({ sel: cssPath(el), right: Math.round(r.right), left: Math.round(r.left), amt: Math.round(amt) });
    }
    // 文字被裁（nowrap + hidden）
    const hasText = el.children.length === 0 && (el.textContent || '').trim().length > 0;
    if (hasText && /(hidden|clip)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 2) {
      clipped.push({ sel: cssPath(el), text: (el.textContent || '').trim().slice(0, 30), scrollW: el.scrollWidth, clientW: el.clientWidth });
    }
    // 过小字号
    if (hasText) {
      const fs = parseFloat(cs.fontSize);
      if (fs && fs < 12) tiny.push({ sel: cssPath(el), fs, text: (el.textContent || '').trim().slice(0, 24) });
    }
    // 触控目标
    if (el.matches('button, a, [role="button"], input, select, textarea, .topnav-link')) {
      if (r.width < 30 || r.height < 30) {
        smallTargets.push({ sel: cssPath(el), w: Math.round(r.width), h: Math.round(r.height), text: (el.textContent || '').trim().slice(0, 18) });
      }
    }
  }
  // 去重同选择器
  const uniq = (arr, k) => {
    const seen = new Set();
    return arr.filter((x) => { const key = x[k] || x.sel; if (seen.has(key)) return false; seen.add(key); return true; }).slice(0, 8);
  };
  // 底部导航是否压住内容
  const dock = document.querySelector('.bottom-dock');
  let dockInfo = null;
  if (dock) {
    const dr = dock.getBoundingClientRect();
    dockInfo = { bottom: Math.round(dr.bottom), top: Math.round(dr.top), h: Math.round(dr.height), position: getComputedStyle(dock).position };
  }
  // 页面主体高度
  const shell = document.querySelector('.app-shell');
  return {
    vw, vh, docW,
    hOverflow: docW - vw,
    overflow: uniq(overflow.sort((a, b) => b.amt - a.amt), 'sel'),
    clipped: uniq(clipped, 'sel'),
    tiny: uniq(tiny, 'sel'),
    smallTargets: uniq(smallTargets, 'sel'),
    dock: dockInfo,
    bodyH: document.body.scrollHeight,
    shellClass: shell ? shell.className : null,
    textLen: (document.body.innerText || '').trim().length,
  };
};

const browser = await chromium.launch();
const report = { generatedAt: new Date().toISOString(), planId: PLAN_ID, results: [] };

for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({
    viewport: { width: vp.w, height: vp.h },
    isMobile: !!vp.mobile,
    hasTouch: !!(vp.mobile || vp.touch),
    deviceScaleFactor: vp.mobile ? 2 : 1,
    userAgent: vp.mobile
      ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
      : undefined,
  });
  await ctx.addInitScript(([t, u]) => {
    if (t) localStorage.setItem('auth', JSON.stringify({ token: t, username: u }));
  }, [TOKEN, USER]);

  for (const pg of PAGES) {
    const page = await ctx.newPage();
    const errors = [];
    const failed = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
    page.on('pageerror', (e) => errors.push('PAGEERROR: ' + String(e.message).slice(0, 200)));
    page.on('requestfailed', (r) => failed.push(`${r.method()} ${r.url().replace(BASE, '')} :: ${r.failure()?.errorText}`));
    page.on('response', (r) => {
      if (r.status() >= 400) failed.push(`HTTP ${r.status()} ${r.url().replace(BASE, '')}`);
    });
    let data = null;
    try {
      await page.goto(BASE + pg.url, { waitUntil: 'networkidle', timeout: 45000 });
      await page.waitForTimeout(vp.mobile ? 900 : 700);
      data = await page.evaluate(probe);
    } catch (e) {
      errors.push('NAV: ' + String(e.message).slice(0, 200));
    }
    const shot = path.join(OUT, `${vp.name}__${pg.key}.png`);
    try { await page.screenshot({ path: shot, fullPage: false }); } catch {}
    try { await page.screenshot({ path: shot.replace('.png', '__full.png'), fullPage: true }); } catch {}
    report.results.push({ viewport: vp.name, page: pg.key, ...data, errors, failed: [...new Set(failed)] });
    console.log(`${vp.name.padEnd(14)} ${pg.key.padEnd(9)} overflow=${data ? data.hOverflow : '?'} clip=${data ? data.clipped.length : '?'} tiny=${data ? data.tiny.length : '?'} tap=${data ? data.smallTargets.length : '?'} err=${errors.length} fail=${[...new Set(failed)].length}`);
    await page.close();
  }
  await ctx.close();
}

await browser.close();
fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
console.log('\nreport -> preview/verify/report.json');
