// Regenerates the README screenshots from a running demo: `python -m vcm_api --demo --port 8779`,
// then `node tests/ui/tour.js "<the Open: URL it printed>" docs/img`.
const { chromium } = require('playwright');
(async () => {
  const [url, out] = [process.argv[2], process.argv[3] || 'docs/img'];
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 800 } });
  await p.goto(url);
  await p.waitForSelector('#repo-meta .rolechip', { timeout: 15000 });
  const shots = [['compare', 'compare'], ['lineage', 'lineage'], ['review', 'review']];
  for (const [view, name] of shots) {
    await p.click(`nav.rail a[data-view=${view}]`);
    await p.waitForTimeout(2500);
    await p.screenshot({ path: `${out}/${name}.png` });
  }
  await b.close();
})();
