import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { runPageSweep } from './page-sweep.mjs';

test('a failed control keeps the earlier observations and stops dependent clicks', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'audit-sweep-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const output = join(directory, 'actions.json');
  let closed = false;
  const page = { on() {}, goto() {}, url: () => 'https://example.test/settings' };
  const chromium = { launch: async () => ({
    newContext: async () => ({ newPage: async () => page }),
    close: async () => { closed = true; }
  }) };
  await assert.rejects(runPageSweep({ chromium, origin: 'https://example.test', output }, async visit => {
    await visit('/settings', async (_page, check) => {
      await check('first', async () => true);
      await check('second', async () => false);
      await check('third', () => assert.fail());
    });
  }), /assertion returned false/);
  const log = JSON.parse(await readFile(output));
  assert.equal(closed, true);
  assert.equal(log.complete, false);
  assert.deepEqual(log.visits[0].actions.map(a => [a.id, a.verified]), [['first', true], ['second', false]]);
});
