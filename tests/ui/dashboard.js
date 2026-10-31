// Drives the dashboard against a seeded API (see tests/test_dashboard_ui.py).
const { chromium } = require('playwright');
const B = process.argv[2];
const SP = process.argv[3] || '';  // optional: directory for screenshots
const log = (...a) => console.log(...a);
const shot = (p, name) => (SP ? p.screenshot({ path: SP + '/' + name }) : null);
let fails = 0;
const check = (name, ok, extra) => { if (!ok) fails++; log((ok ? 'PASS ' : 'FAIL ') + name + (extra ? '  ' + extra : '')); };

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  async function page(ctx) {
    const p = await ctx.newPage();
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error' && !/401|403|400/.test(m.text())) errors.push(m.text()); });
    return p;
  }
  const mk = () => browser.newContext({ viewport: { width: 1280, height: 900 } });
  const txt = (p, sel) => p.locator(sel).first().innerText();

  // ---- sign-in: wrong token, then right token (maintainer)
  const ctxA = await mk(); const A = await page(ctxA);
  await A.goto(B + '/?theme=dark');
  await A.waitForSelector('#tok');
  check('sign-in screen shown without a token', await A.locator('.signin').count() === 1);
  await shot(A, '01-signin.png');
  await A.fill('#tok', 'wrong-token-0123456789'); await A.click('.signin button.primary');
  await A.waitForSelector('.signin .err');
  check('wrong token is rejected with a message', /not accepted/.test(await txt(A, '.signin .err')));
  await A.fill('#tok', 'alice-token-0123456789'); await A.press('#tok', 'Enter');
  await A.waitForSelector('#repo-meta .rolechip');
  const meta = await txt(A, '#repo-meta');
  check('topbar shows identity and role', /alice/.test(meta) && /maintainer/i.test(meta), meta.replace(/\s+/g, ' '));
  check('topbar shows store backend', /store\s*local/i.test(meta));
  await A.waitForFunction(() => /polling/.test(document.getElementById('live-txt').textContent));
  check('live indicator says polling', true);

  // ---- locks view
  await A.click('nav.rail a[data-view=locks]');
  await A.waitForSelector('#lk-out table');
  check('existing lock listed with holder', /carol/.test(await txt(A, '#lk-out')));
  await A.fill('#lk-path', 'board/main.kicad_pcb'); await A.fill('#lk-note', 'routing pass');
  await A.click('button.primary:text-is("Lock")');
  await A.waitForFunction(() => /main\.kicad_pcb/.test(document.getElementById('lk-out').innerText));
  const row = A.locator('#lk-out tr', { hasText: 'main.kicad_pcb' });
  check('new lock is mine (shows "you")', /you/.test(await row.innerText()));
  // a conflicting lock is explained, not swallowed
  await A.fill('#lk-path', 'firmware/driver.conf'); await A.click('button.primary:text-is("Lock")');
  await A.waitForSelector('#lk-msg .err');
  check('conflict shows the server message', /locked by carol/.test(await txt(A, '#lk-msg')));
  await shot(A, '02-locks.png');

  // ---- live: another client changes state; this page updates without a click
  const ctxB = await mk(); const Bp = await page(ctxB);
  await Bp.goto(B + '/'); await Bp.fill('#tok', 'bob-token-0123456789'); await Bp.press('#tok', 'Enter');
  await Bp.waitForSelector('#repo-meta .rolechip');
  check('writer role shown', /writer/i.test(await txt(Bp, '#repo-meta')));
  await Bp.click('nav.rail a[data-view=locks]'); await Bp.waitForSelector('#lk-out table');
  await Bp.fill('#lk-path', 'docs/notes.md'); await Bp.click('button.primary:text-is("Lock")');
  await A.waitForFunction(() => /notes\.md/.test(document.getElementById('lk-out').innerText), null, { timeout: 15000 });
  check("alice's page picked up bob's lock live", true);
  check('activity feed lists it with the actor', /bob/.test(await txt(A, '#lk-feed')));
  // writer cannot break someone else's lock
  const breakBtn = Bp.locator('#lk-out tr', { hasText: 'driver.conf' }).locator('button');
  check('writer cannot break carol\'s lock (disabled)', await breakBtn.isDisabled());
  // maintainer can
  A.once('dialog', (d) => d.accept());
  await A.locator('#lk-out tr', { hasText: 'driver.conf' }).locator('button').click();
  await A.waitForFunction(() => !/driver\.conf/.test(document.getElementById('lk-out').innerText), null, { timeout: 10000 });
  check('maintainer broke carol\'s lock', true);

  // ---- releases: signature state
  await A.click('nav.rail a[data-view=releases]');
  await A.waitForSelector('#rl-out table');
  const rl = await txt(A, '#rl-out');
  check('release list shows signed vs unsigned', /1 signature/i.test(rl) && /unsigned/i.test(rl));
  await A.locator('#rl-out tr', { hasText: 'R1' }).first().locator('button', { hasText: 'Verify' }).click();
  await A.waitForSelector('#rl-detail .statuschip');
  check('verify reports authenticated: bot', /authenticated: bot/i.test(await txt(A, '#rl-detail')));
  await A.fill('#rl-key', 'bot');
  await A.locator('#rl-out tr', { hasText: 'R2' }).first().locator('button', { hasText: 'Sign' }).click();
  await A.waitForFunction(() => document.querySelectorAll('#rl-out tbody tr').length && ![...document.querySelectorAll('#rl-out tbody tr')].some(r => /unsigned/i.test(r.innerText)), null, { timeout: 15000 }).catch(() => {});
  const rl2 = await txt(A, '#rl-out');
  check('maintainer signed R2 from the UI (key lives on the server)', !/unsigned/i.test(rl2), /unsigned/i.test(rl2) ? 'still unsigned' : '');
  await shot(A, '03-releases.png');

  // ---- review: writer can approve but not merge; maintainer merges
  await Bp.click('nav.rail a[data-view=review]'); await Bp.waitForSelector('#pr-detail .verdict');
  const merge = Bp.locator('button', { hasText: 'Merge (fast-forward)' });
  check('merge button disabled for a writer', await merge.isDisabled());
  check('writer sees blocker (needs approval)', /needs 1 more approval/i.test(await txt(Bp, '#pr-detail .verdict')));
  await A.click('nav.rail a[data-view=review]'); await A.waitForSelector('#pr-detail .verdict');
  await A.click('button:has-text("Approve as alice")');
  await A.waitForFunction(() => /Ready to merge|Approved/i.test((document.querySelector('#pr-detail .verdict')||{}).innerText||''));
  check('approval recorded under the token identity', /alice/.test(await txt(A, '#pr-detail dl')));
  await shot(A, '04-review.png');
  // bob's page learns about the approval live
  await Bp.waitForFunction(() => /Ready to merge|Approved/i.test((document.querySelector('#pr-detail .verdict')||{}).innerText||''), null, { timeout: 15000 });
  check("bob's page updated live after alice approved", true);
  check('still disabled for the writer after approval', await merge.isDisabled());
  await A.click('button:has-text("Merge (fast-forward)")');
  await A.waitForFunction(() => /Merged into main by alice/.test((document.querySelector('#pr-detail .verdict')||{}).innerText||''), null, { timeout: 10000 });
  check('maintainer merged: "Merged into main by alice"', true);
  await shot(A, '05-merged.png');

  // ---- reader: sees everything, can change nothing
  const ctxR = await mk(); const R = await page(ctxR);
  await R.goto(B + '/?theme=light'); await R.fill('#tok', 'rita-token-0123456789'); await R.press('#tok', 'Enter');
  await R.waitForSelector('#repo-meta .rolechip');
  await R.click('nav.rail a[data-view=locks]'); await R.waitForSelector('#lk-out table');
  check('reader: Lock button disabled', await R.locator('button.primary', { hasText: 'Lock' }).isDisabled());
  await shot(R, '06-reader-light.png');

  // ---- sign out
  await A.click('text=Sign out'); await A.waitForSelector('.signin');
  check('sign out returns to the sign-in screen', true);

  check('no uncaught page errors', errors.length === 0, errors.slice(0, 3).join(' | '));
  await browser.close();
  log(fails ? `\n${fails} FAILED` : '\nall UI checks passed');
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error('SCRIPT ERROR', e); process.exit(2); });
