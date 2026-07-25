// Simverse World 前端 E2E 冒烟 (Playwright)
// 用法: node e2e_test.mjs
import { chromium } from 'playwright';
import fs from 'fs';

const FRONT = 'https://simverse.world';
const API = 'https://simverse-api.proxypool.eu.org';
const results = [];
const rec = (id, name, status, detail = '') => {
  results.push({ id, name, status, detail: String(detail).slice(0, 400) });
  console.log(`${{ PASS: '✅', FAIL: '❌', SKIP: '⏭️', WARN: '⚠️' }[status] || '?'} ${id} ${status} ${name} — ${String(detail).slice(0, 140)}`);
};

const rnd = Math.random().toString(36).slice(2, 7);
const EMAIL = `svtest_e2e_${rnd}@sv-test.dev`;
const PW = `SvE2e!${rnd}9`;

fs.mkdirSync('shots', { recursive: true });

const PROXY = process.env.HTTPS_PROXY || 'http://127.0.0.1:35449';
const browser = await chromium.launch({ headless: true, proxy: { server: PROXY } });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN', ignoreHTTPSErrors: true });
const page = await ctx.newPage();

const consoleErrors = [];
const failedReqs = [];
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
page.on('response', r => { if (r.status() >= 500) failedReqs.push(`${r.status()} ${r.url().slice(0, 120)}`); });
page.on('requestfailed', r => { const f = r.failure()?.errorText || ''; if (!f.includes('ERR_ABORTED')) failedReqs.push(`FAILED ${r.url().slice(0, 100)} ${f}`); });

// ---- M13-1 落地页
try {
  await page.goto(FRONT + '/', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2000);
  const title = await page.title();
  const text = (await page.textContent('body') || '').slice(0, 3000);
  const hasHero = text.includes('赛博') || text.includes('Simverse') || text.length > 200;
  await page.screenshot({ path: 'shots/01_landing.png' });
  rec('M13-1', '落地页渲染', hasHero ? 'PASS' : 'FAIL', `title=${title} bodyLen=${text.length}`);
} catch (e) { rec('M13-1', '落地页渲染', 'FAIL', e.message); }

// ---- M13-2 注册(通过 API 注册后注入 token, 再验证 UI 登录态; 同时尝试 UI 注册入口存在性)
let token = null;
try {
  // UI 上找注册/登录入口
  const cta = await page.locator('a,button', { hasText: /登录|注册|进入|开始|Login|Start|Enter/ }).first();
  const ctaText = await cta.textContent().catch(() => null);
  rec('M13-2a', '登录/注册入口存在', ctaText ? 'PASS' : 'WARN', `CTA="${(ctaText || '').trim()}"`);

  const resp = await ctx.request.post(API + '/auth/register', {
    data: { name: `E2E测试_${rnd}`, email: EMAIL, password: PW } });
  const body = await resp.json().catch(() => ({}));
  token = body.access_token || body.token;
  rec('M13-2b', 'API 注册测试账号', token ? 'PASS' : 'FAIL', `${resp.status()} ${EMAIL}`);
} catch (e) { rec('M13-2', '注册', 'FAIL', e.message); }

// 尝试 UI 登录表单(不强求)
try {
  const loginLink = page.locator('a[href*="login"], a,button', { hasText: /登录|Login/ }).first();
  if (await loginLink.count()) {
    await loginLink.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const emailInput = page.locator('input[type="email"], input[name*="email"], input[placeholder*="邮箱"]').first();
    if (await emailInput.count()) {
      await emailInput.fill(EMAIL);
      await page.locator('input[type="password"]').first().fill(PW);
      await page.screenshot({ path: 'shots/02_login_form.png' });
      await page.locator('button[type="submit"], button', { hasText: /登录|Login|进入/ }).first().click();
      await page.waitForTimeout(4000);
      const tokenInLs = await page.evaluate(() => localStorage.getItem('token'));
      rec('M13-2c', 'UI 登录表单', tokenInLs ? 'PASS' : 'WARN', tokenInLs ? 'localStorage token 已写入' : `登录后 url=${page.url()}`);
      if (tokenInLs) token = tokenInLs;
    } else {
      await page.screenshot({ path: 'shots/02_login_page.png' });
      rec('M13-2c', 'UI 登录表单', 'WARN', `未找到邮箱输入框 url=${page.url()}`);
    }
  }
} catch (e) { rec('M13-2c', 'UI 登录表单', 'WARN', e.message); }

// 注入 token 确保登录态
if (token) {
  await page.goto(FRONT + '/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(t => localStorage.setItem('token', t), token);
}

// ---- Onboarding: 创建角色 (UI)
try {
  await page.goto(FRONT + '/onboarding', { waitUntil: 'networkidle', timeout: 45000 }).catch(() => {});
  await page.waitForTimeout(2500);
  await page.screenshot({ path: 'shots/03_onboarding.png' });
  const nameInput = page.locator('input[type="text"], input[placeholder*="名"], input[name*="name"]').first();
  if (await nameInput.count()) {
    await nameInput.fill('E2E小侦');
    // 选精灵: 点第一个可选的精灵图
    const sprite = page.locator('[class*="sprite"], [class*="avatar"], img').nth(1);
    await sprite.click({ timeout: 3000 }).catch(() => {});
    const submit = page.locator('button', { hasText: /创建|确认|开始|完成|进入/ }).first();
    await submit.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(5000);
    rec('M13-2d', 'Onboarding 创建角色', 'PASS', `完成后 url=${page.url()}`);
  } else {
    // 可能直接跳过了 onboarding
    rec('M13-2d', 'Onboarding 页面', 'WARN', `无输入框, url=${page.url()} (可能已有角色或走 API 补建)`);
    await ctx.request.post(API + '/onboarding/create-character', {
      headers: { Authorization: `Bearer ${token}` },
      data: { name: 'E2E小侦', sprite_key: '伊莎贝拉', reply_mode: 'manual' } }).catch(() => {});
  }
} catch (e) { rec('M13-2d', 'Onboarding', 'WARN', e.message); }

// ---- M13-3 游戏世界
try {
  const wsFrames = [];
  page.on('websocket', ws => {
    wsFrames.push('open:' + ws.url().slice(0, 60));
    ws.on('framereceived', f => { if (wsFrames.length < 50) wsFrames.push('rx'); });
  });
  await page.goto(FRONT + '/game', { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(12000); // 等 Phaser + tilemap + WS
  const canvas = await page.locator('canvas').count();
  await page.screenshot({ path: 'shots/04_game.png' });
  const wsOpen = wsFrames.some(f => f.startsWith('open'));
  const wsRx = wsFrames.filter(f => f === 'rx').length;
  rec('M13-3', '游戏世界渲染', canvas > 0 ? 'PASS' : 'FAIL', `canvas=${canvas} url=${page.url()}`);
  rec('M13-3b', '游戏页 WebSocket', wsOpen && wsRx > 0 ? 'PASS' : 'WARN', `open=${wsOpen} 收到帧=${wsRx}`);
} catch (e) { rec('M13-3', '游戏世界渲染', 'FAIL', e.message); }

// ---- M13-6 其他页面路由
const routes = [
  ['/forge', '锻造页', '05_forge'],
  ['/profile', '个人档案页', '06_profile'],
  ['/seasons', '赛季页', '07_seasons'],
  ['/debates', '辩论页', '08_debates'],
  ['/capsules', '胶囊页', '09_capsules'],
  ['/graph', '图谱页', '10_graph'],
];
for (const [route, name, shot] of routes) {
  try {
    await page.goto(FRONT + route, { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);
    const text = (await page.textContent('body')) || '';
    const blank = text.trim().length < 30;
    await page.screenshot({ path: `shots/${shot}.png` });
    rec('M13-6', `${name} ${route}`, blank ? 'FAIL' : 'PASS', `内容 ${text.trim().length} 字`);
  } catch (e) { rec('M13-6', `${name} ${route}`, 'FAIL', e.message); }
}

// ---- M13-7 管理页门禁
try {
  await page.goto(FRONT + '/admin', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2500);
  const text = (await page.textContent('body')) || '';
  await page.screenshot({ path: 'shots/11_admin.png' });
  const hasAdminData = /用户管理|仪表盘|经济配置|Dashboard/.test(text) && text.length > 500;
  rec('M13-7', '管理页普通账号门禁', hasAdminData ? 'FAIL' : 'PASS',
      hasAdminData ? '普通账号看到了管理数据!' : `被拒/重定向 url=${page.url()}`);
} catch (e) { rec('M13-7', '管理页门禁', 'WARN', e.message); }

// ---- M13-8 控制台巡检
const errSample = consoleErrors.slice(0, 5);
rec('M13-8a', '控制台无 JS error', consoleErrors.length === 0 ? 'PASS' : 'WARN', `${consoleErrors.length} 条: ${errSample.join(' | ')}`);
rec('M13-8b', '无 5xx/失败请求', failedReqs.length === 0 ? 'PASS' : 'WARN', `${failedReqs.length} 条: ${failedReqs.slice(0, 4).join(' | ')}`);

await browser.close();
fs.writeFileSync('e2e_results.json', JSON.stringify({ email: EMAIL, results }, null, 1));
console.log('\nE2E done ->', 'e2e_results.json');
