// Run a batch of known controls in one Playwright session. The caller supplies
// normal Playwright actions and assertions, and owns fixture cleanup.
import { open } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';

export async function runPageSweep({ chromium, origin, contextOptions, output, role, build }, run) {
  const target = new URL(origin).origin;
  const file = await open(output, 'wx', 0o600);
  const start = performance.now();
  const report = { role, build, visits: [], errors: [], complete: false };
  let browser;
  try {
    browser = await chromium.launch();
    const context = await browser.newContext(contextOptions);
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push({ type: 'pageerror', name: error.name }));
    page.on('response', response => {
      if (response.status() >= 400 && new URL(response.url()).origin === target) {
        report.errors.push({ type: 'http', method: response.request().method(), status: response.status() });
      }
    });
    await run(async (path, controls) => {
      const url = new URL(path, target);
      if (url.origin !== target) throw new Error('Visit outside audit origin');
      const visit = { path: url.pathname, actions: [], complete: false };
      report.visits.push(visit);
      await page.goto(url.href, { waitUntil: 'domcontentloaded' });
      const check = async (id, actionAndAssertion) => {
        const t = performance.now();
        const entry = { id, verified: false };
        visit.actions.push(entry);
        try {
          if (new URL(page.url()).origin !== target) throw new Error('Unexpected origin');
          if (await actionAndAssertion() === false) throw new Error('Control assertion returned false');
          if (new URL(page.url()).origin !== target) throw new Error('Unexpected origin');
          entry.verified = true;
        } finally {
          entry.ms = Math.round(performance.now() - t);
        }
      };
      await controls(page, check);
      visit.complete = true;
    });
    report.complete = true;
  } finally {
    try {
      await browser?.close();
    } finally {
      report.elapsedMs = Math.round(performance.now() - start);
      try { await file.writeFile(JSON.stringify(report, null, 2)); }
      finally { await file.close(); }
    }
  }
  return report;
}
