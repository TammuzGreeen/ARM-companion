const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({httpCredentials:{username:'operator',password:'synthetic-browser-only'}});
  const base = process.env.REVIEW_URL || 'http://127.0.0.1:18081';
  const fixture = await (await page.request.get(base + '/api/status')).json();
  const original = fixture.cycles[0];
  let revision = 1, delay = 0, detailDelay = 0, active = 0, maxActive = 0, statusCalls = 0, detailCalls = 0, images = 0;
  let structural = false, captureCount = 1, removeFirst = false, failNext = false;
  let deletionStatus = null;
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  const rows = () => Array.from({length:24}, (_, i) => ({...original,id:'synthetic-' + i,updated:i ? 1 : revision,
    technical_error:structural ? 'Synthetic technical failure' : null,
    fallback_unavailable:structural ? 'Synthetic fallback notice' : null,
    deletion:deletionStatus ? {status:deletionStatus} : null,
    sources:structural ? [{...original.sources[0],source_id:999},...original.sources] : original.sources,
    snapshots:Array.from({length:captureCount}, (_, n) => `capture-${n}.jpg`)})).filter((c,i) => !removeFirst || i !== 0);
  await page.route('**/api/status', async route => {
    active++; maxActive = Math.max(maxActive, active); statusCalls++;
    const failed = failNext; failNext = false;
    const body = failed ? JSON.stringify({detail:'obsolete failure'}) : JSON.stringify({...fixture,cycles:rows(),error:'revision-' + revision});
    if (delay) await new Promise(resolve => setTimeout(resolve, delay));
    await route.fulfill({status:failed ? 500 : 200,contentType:'application/json',body}); active--;
  });
  await page.route(/\/api\/cycles\/synthetic-\d+$/, async route => {
    detailCalls++;
    const id = route.request().url().split('/').at(-1);
    const detail = {...rows().find(c=>c.id===id),locals:[{model:'synthetic',findings:{text:'retained-' + revision}}]};
    if (detailDelay) await new Promise(resolve => setTimeout(resolve, detailDelay));
    await route.fulfill({contentType:'application/json',body:JSON.stringify(detail)});
  });
  await page.route('**/evidence/**', route => { images++; return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'}); });
  await page.goto(base);
  await page.locator('#cycles > article').nth(23).waitFor();
  await page.evaluate(() => { clearTimeout(pollTimer); window.kept = document.querySelector('#cycles > article'); window.other = document.querySelectorAll('#cycles > article')[1]; });
  assert.equal(images, 0); assert.equal(detailCalls, 0);
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  assert.equal(await page.evaluate(() => kept === document.querySelector('#cycles > article')), true);
  const first = page.locator('#cycles > article').first();
  await first.locator('[data-findings] > summary').click();
  await first.locator('[data-lazy]').getByText('Local observations',{exact:true}).waitFor();
  await first.locator('[data-lazy] details > summary').click();
  await first.locator('[data-captures] > summary').click();
  await first.locator('img').waitFor({state:'visible'});
  await page.waitForFunction(() => document.querySelector('#cycles img').complete);
  const imagesOnce = images;
  await first.locator('tbody details > summary').first().click();
  await first.locator('[data-review]').focus();
  revision++; structural = true;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  await first.locator('[data-lazy]').getByText(/retained-2/).waitFor();
  assert.equal(await page.evaluate(() => kept === document.querySelector('#cycles > article') && other === document.querySelectorAll('#cycles > article')[1]), true);
  assert.equal(await first.locator('[data-findings]').evaluate(e => e.open), true);
  assert.equal(await first.locator('[data-lazy] details').evaluate(e => e.open), true);
  assert.equal(await first.locator(`tr[data-key="source-${original.sources[0].source_id}"] details`).evaluate(e => e.open), true);
  assert.equal(await first.locator('[data-review]').evaluate(e => e === document.activeElement), true);
  await first.locator('a[href$="/record/md"]').focus();
  deletionStatus = 'preview'; revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  assert.equal(await first.locator('a[href$="/record/md"]').evaluate(e => e === document.activeElement), true);
  assert.equal(await first.locator('[data-findings]').evaluate(e => e.open), true);
  deletionStatus = null; revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  assert.equal(images, imagesOnce);
  await first.locator('[data-review]').focus();
  structural = false; revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  assert.equal(await first.locator(`tr[data-key="source-${original.sources[0].source_id}"] details`).evaluate(e => e.open), true);
  assert.equal(await first.locator('[data-review]').evaluate(e => e === document.activeElement), true);
  // Newly arriving captures activate in an already-open panel, without reloading old images.
  captureCount = 2; revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  await page.waitForFunction(() => [...document.querySelectorAll('#cycles > article:first-child img')].every(i => i.hasAttribute('src') && i.complete));
  assert.equal(images, imagesOnce + 1);
  // Empty -> one capture in an open panel also activates without another toggle.
  captureCount = 0; revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  captureCount = 1; revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  await page.waitForFunction(() => document.querySelector('#cycles img')?.hasAttribute('src'));
  // This synthetic cycle has no server-side review. Use an existing fixture ID to
  // test drafts via the normal authenticated review flow instead.
  await page.evaluate(async id => { cycles[0].id = id; await edit(id); }, original.id);
  await page.locator('#review[open]').waitFor();
  await page.locator('[data-field="series"]').fill('Unsaved performance draft');
  revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  assert.equal(await page.locator('[data-field="series"]').inputValue(), 'Unsaved performance draft');
  await page.locator('#cancel').click();
  // A superseding refresh invalidates a delayed response; follow-up stays single-flight.
  delay = 350;
  const before = statusCalls;
  await page.evaluate(() => { clearTimeout(pollTimer); refresh(); });
  await page.waitForTimeout(50); revision++;
  await page.evaluate(() => refresh());
  await page.waitForFunction(expected => document.querySelector('#error').textContent === expected, 'revision-' + revision);
  await page.waitForTimeout(800);
  await page.evaluate(() => clearTimeout(pollTimer));
  assert.equal(maxActive, 1); assert.ok(statusCalls >= before + 2);
  failNext = true;
  await page.evaluate(() => refresh());
  await page.waitForTimeout(50); revision++;
  await page.evaluate(() => refresh());
  await page.waitForTimeout(350);
  assert.notEqual(await page.locator('#error').innerText(), 'obsolete failure');
  await page.waitForFunction(expected => document.querySelector('#error').textContent === expected, 'revision-' + revision);
  await page.evaluate(() => clearTimeout(pollTimer)); delay = 0;
  // Delayed detail cannot populate a collapsed, superseded or removed card.
  await first.locator('[data-findings] > summary').click();
  detailDelay = 350; revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  await first.locator('[data-findings] > summary').click();
  await page.waitForTimeout(50);
  await first.locator('[data-findings] > summary').click();
  await page.waitForTimeout(400);
  assert.equal(await first.locator('[data-findings]').evaluate(e => e.open), false);
  await first.locator('[data-findings] > summary').click();
  await page.waitForTimeout(50); revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  await first.locator('[data-lazy]').getByText(new RegExp('retained-' + revision)).waitFor();
  revision++;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  removeFirst = true;
  await page.evaluate(async () => { await refresh(); clearTimeout(pollTimer); });
  await page.waitForTimeout(450);
  assert.equal(await page.locator('article[data-id="synthetic-0"]').count(), 0);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log(JSON.stringify({result:'passed',cycles:24,maxConcurrentPolls:maxActive,images,detailCalls,statusCalls}));
})().catch(error => { console.error(error); process.exit(1); });
