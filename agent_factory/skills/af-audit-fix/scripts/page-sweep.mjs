// Run a batch of known controls in one Playwright session. The caller supplies
// normal Playwright actions and assertions, and owns fixture cleanup.
import { execFileSync } from 'node:child_process';
import { open } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';

export async function runPageSweep({ chromium, origin, contextOptions, output, role, build, batchStartedAt = Number(process.env.AF_AUDIT_BATCH_STARTED_MS), sport, viewport, fixtureState, ledgerCommand, recordSweep }, run) {
  const target = new URL(origin).origin;
  const file = await open(output, 'wx', 0o600);
  const start = performance.now();
  const report = { origin: target, role, build, sport, viewport, fixtureState, batchStartedAt, browserStartedAt: Date.now(), remaining: {}, visits: [], errors: [], complete: false };
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
        const structured = typeof id === 'object' && id !== null;
        const entry = structured ? { ...id, verified: false } : { id, verified: false };
        visit.actions.push(entry);
        try {
          if (new URL(page.url()).origin !== target) throw new Error('Unexpected origin');
          const observed = await actionAndAssertion();
          if (structured && (typeof observed !== 'string' || !observed.trim())) throw new Error('Structured checks require an observed outcome');
          if (structured) entry.actual = observed;
          if (observed === false) throw new Error('Control assertion returned false');
          if (new URL(page.url()).origin !== target) throw new Error('Unexpected origin');
          entry.verified = true;
        } catch (error) {
          if (structured) entry.actual = error.name || 'Error';
          throw error;
        } finally {
          entry.ms = Math.round(performance.now() - t);
        }
      };
      await controls(page, check, (caseId, steps) => {
        if (!Array.isArray(steps) || !steps.every(step => typeof step === 'string' && step.trim())) {
          throw new Error('Remaining checks must be a list of descriptions');
        }
        report.remaining[caseId] = steps;
      });
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
      if (ledgerCommand && report.visits.some(v => v.actions.some(a => a.caseId))) {
        const [command, ...args] = ledgerCommand;
        report.receipt = JSON.parse(execFileSync(command, [...args, 'record-sweep', '--input', output], { encoding: 'utf8' }));
      }
      if (recordSweep) await recordSweep(output);
    }
  }
  return report;
}
