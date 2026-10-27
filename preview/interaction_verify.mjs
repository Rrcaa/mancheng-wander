// 交互深检：滚动到底 / 遮挡命中测试 / 点击流程 / 横屏
import pw from 'file:///Users/xu/.workbuddy/binaries/node/workspace/node_modules/playwright/index.js';
const { chromium } = pw;
import fs from 'node:fs';
import path from 'node:path';

const BASE = 'http://localhost:8765';
const PLAN_ID = process.env.PLAN_ID;
const TOKEN = process.env.PROBE_TOKEN;
const OUT = path.resolve('preview/verify');
fs.mkdirSync(OUT, { recursive: true });

const VP = [
  { name: 'desktop-1440', w: 1440, h: 900, mobile: false },
  { name: 'mobile-390', w: 390, h: 844, mobile: true },
  { name: 'mobile-320', w: 320, h: 568, mobile: true },
  { name: 'mobile-landscape', w: 844, h: 390, mobile: true },
];

const out = {};

const occludedProbe = () => {
  // 命中测试：交互元素中心点是否被别的元素盖住
  const res = [];
  const els = [...document.querySelectorAll('button, a, [role="button"], input, textarea, .topnav-link, .rd-stop')];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > innerHeight) continue; // 视口外不算
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.pointerEvents === 'none') continue;
    const cx = Math.min(innerWidth - 2, Math.max(2, r.left + r.width / 2));
    const cy = Math.min(innerHeight - 2, Math.max(2, r.top + r.height / 2));
    const hit = document.elementFromPoint(cx, cy);
    if (!hit) continue;
    if (hit === el || el.contains(hit) || hit.contains(el)) continue;
    const hs = getComputedStyle(hit);
    if (hs.pointerEvents === 'none') continue;
    res.push({
      target: (el.className || el.tagName) + ' :: ' + (el.textContent || '').trim().slice(0, 20),
      blocker: (hit.className || hit.tagName) + ' :: ' + (hit.textContent || '').trim().slice(0, 20),
      box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    });
  }
  return res.slice(0, 10);
};

const bottomGapProbe = () => {
  // 页面最底部内容与固定导航的关系
  const dock = document.querySelector('.bottom-dock');
  const doc = document.scrollingElement || document.documentElement;
  const atBottom = Math.abs(doc.scrollHeight - doc.clientHeight - doc.scrollTop) < 4;
  return {
    scrollH: doc.scrollHeight,
    clientH: doc.clientHeight,
    atBottom,
    dockRect: dock ? (({ top, bottom, height }) => ({ top: Math.round(top), bottom: Math.round(bottom), height: Math.round(height) }))(dock.getBoundingClientRect()) : null,
    dockPos: dock ? getComputedStyle(dock).position : null,
    bodyPadBottom: getComputedStyle(document.body).paddingBottom,
  };
};

const browser = await chromium.launch();
for (const vp of VP) {
  const ctx = await browser.newContext({
    viewport: { width: vp.w, height: vp.h },
    isMobile: vp.mobile,
    hasTouch: vp.mobile,
    deviceScaleFactor: vp.mobile ? 2 : 1,
    userAgent: vp.mobile ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1' : undefined,
  });
  await ctx.addInitScript(([t]) => {
    if (t) localStorage.setItem('auth', JSON.stringify({ token: t, username: 'probe_ui_01' }));
  }, [TOKEN]);

  const rec = {};
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e.message).slice(0, 160)));

  // ---- 首页 ----
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(600);
  rec.homeOccluded = await page.evaluate(occludedProbe);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.waitForTimeout(400);
  rec.homeBottom = await page.evaluate(bottomGapProbe);
  await page.screenshot({ path: path.join(OUT, `${vp.name}__home-bottom.png`) });

  // 城市横向滚动条是否真的可滚
  rec.cityRow = await page.evaluate(() => {
    const row = [...document.querySelectorAll('*')].find((e) => e.textContent?.includes('解析攻略') && e.children.length > 2 && e.scrollWidth > e.clientWidth + 4);
    return row ? { sel: row.className || row.tagName, scrollW: row.scrollWidth, clientW: row.clientWidth, overflowX: getComputedStyle(row).overflowX } : null;
  });

  // ---- 详情 ----
  await page.goto(BASE + `/?view_plan_id=${PLAN_ID}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  rec.detailOccluded = await page.evaluate(occludedProbe);
  await page.screenshot({ path: path.join(OUT, `${vp.name}__detail-top.png`) });

  // 路由列滚到底
  rec.routeScroll = await page.evaluate(() => {
    const col = document.querySelector('.rd-route-column') || document.querySelector('.rd-timeline')?.parentElement;
    if (!col) return null;
    const before = col.scrollTop;
    col.scrollTop = col.scrollHeight;
    const cs = getComputedStyle(col);
    return { hasScroll: col.scrollHeight > col.clientHeight + 4, before, after: col.scrollTop, padBottom: cs.paddingBottom, overflowY: cs.overflowY };
  });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, `${vp.name}__detail-route-end.png`) });
  rec.detailOccludedAtEnd = await page.evaluate(occludedProbe);

  // Day 2 切换
  rec.daySwitch = await page.evaluate(async () => {
    const tabs = [...document.querySelectorAll('button')].filter((b) => /Day\s*2/.test(b.textContent || ''));
    if (!tabs.length) return { ok: false, reason: 'no day2 tab' };
    const r = tabs[0].getBoundingClientRect();
    return { ok: true, box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)], text: tabs[0].textContent.trim().slice(0, 30) };
  });
  if (rec.daySwitch.ok) {
    await page.evaluate(() => {
      const t = [...document.querySelectorAll('button')].find((b) => /Day\s*2/.test(b.textContent || ''));
      t && t.click();
    });
    await page.waitForTimeout(700);
    rec.afterDay2 = await page.evaluate(() => {
      const h = document.querySelector('.rd-route-column header small, .rd-day-title, .rd-route-column header');
      const stops = [...document.querySelectorAll('.rd-stop')].length;
      return { header: (document.querySelector('.rd-route-column header')?.textContent || '').trim().slice(0, 40), stops };
    });
    await page.screenshot({ path: path.join(OUT, `${vp.name}__detail-day2.png`) });
  }

  // 地图 tab（移动端分段控件）
  rec.mapTab = await page.evaluate(() => {
    const b = [...document.querySelectorAll('button')].find((x) => (x.textContent || '').trim() === '地图');
    if (!b) return { exists: false };
    b.click();
    return { exists: true };
  });
  await page.waitForTimeout(1200);
  rec.mapAfter = await page.evaluate(() => {
    const m = document.querySelector('.amap-container, #map, .rd-map, .map-canvas, canvas');
    if (!m) return { found: false };
    const r = m.getBoundingClientRect();
    return { found: true, sel: m.className || m.tagName, w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top) };
  });
  await page.screenshot({ path: path.join(OUT, `${vp.name}__detail-map.png`) });

  // 底部导航点击
  rec.dockNav = await page.evaluate(async () => {
    const links = [...document.querySelectorAll('.topnav-link')];
    const out = [];
    for (const l of links) out.push({ label: l.textContent.trim(), w: Math.round(l.getBoundingClientRect().width), h: Math.round(l.getBoundingClientRect().height) });
    return out;
  });

  rec.errors = errors;
  out[vp.name] = rec;
  await page.close();
  await ctx.close();
}
await browser.close();
fs.writeFileSync(path.join(OUT, 'interaction.json'), JSON.stringify(out, null, 2));
console.log(JSON.stringify(out, null, 2).slice(0, 6000));
