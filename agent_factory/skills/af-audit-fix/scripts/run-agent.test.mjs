import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, chmodSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { invocation, interpretEvent, runAgent } from './run-agent.mjs';

test('both engine launches keep prompt out of argv and preserve model selection', () => {
  for (const engine of ['codex', 'claude']) {
    const spec = invocation(engine, `/bin/${engine}`, '/tmp/work space & literal', 'default');
    assert.equal(spec.shell, false);
    assert.equal(spec.cwd, '/tmp/work space & literal');
    assert.equal(spec.args.includes('--model'), false);
    assert.equal(spec.args.includes('--dangerously-skip-permissions'), false);
  }
  assert.throws(() => invocation('other', '/bin/x', '/tmp'), /engine/);
  assert.throws(() => invocation('codex', '/bin/x', '/tmp', 'other'), /permissions/);
});

test('explicit capabilities map to each CLI without a silent engine fallback', () => {
  assert.ok(invocation('codex', '/bin/codex', '/tmp', 'workspace-write').args.includes('workspace-write'));
  assert.ok(invocation('claude', '/bin/claude', '/tmp', 'workspace-write').args.includes('acceptEdits'));
  assert.ok(invocation('codex', '/bin/codex', '/tmp', 'full-access').args.includes('danger-full-access'));
  assert.ok(invocation('claude', '/bin/claude', '/tmp', 'full-access').args.includes('bypassPermissions'));
  assert.throws(() => invocation('codex', 'C:\\unknown\\codex.cmd', 'C:\\work', 'default', 'win32'), /JavaScript entry point/);
});

test('semantic provider failures are not mistaken for successful process exits', () => {
  assert.equal(interpretEvent('claude', {type: 'result', subtype: 'error_max_turns', is_error: true}).failed, true);
  assert.equal(interpretEvent('codex', {type: 'turn.failed'}).failed, true);
  assert.deepEqual(interpretEvent('codex', {type: 'error', message: 'Reconnecting... 2/5 (Selected model is at capacity)'}), {});
});

test('one-shot adapter records real stdin, exit, stream and refusal to overwrite', async () => {
  if (process.platform === 'win32') return; // Native Windows smoke exercises its real binaries.
  const root = mkdtempSync(path.join(tmpdir(), 'audit-runner-'));
  try {
    const cliPath = path.join(root, 'fake executable');
    writeFileSync(cliPath, '#!/usr/bin/env node\nlet text="";process.stdin.on("data",x=>text+=x);process.stdin.on("end",()=>{console.log(JSON.stringify({type:"item.completed",item:{type:"agent_message",text}}));console.log(JSON.stringify({type:"turn.completed"}));});\n');
    chmodSync(cliPath, 0o700);
    const promptFile = path.join(root, 'prompt');
    writeFileSync(promptFile, 'Literal $(do not execute) & | `text`');
    const outputDir = path.join(root, 'attempt');
    const options = {engine:'codex', cwd:root, cliPath, promptFile, outputDir, timeoutMs:5000};
    assert.equal(await runAgent(options), 0);
    assert.equal(readFileSync(path.join(outputDir, 'final.txt'), 'utf8'), readFileSync(promptFile, 'utf8'));
    const status = JSON.parse(readFileSync(path.join(outputDir,'process.json')));
    assert.equal(status.exitCode, 0);
    assert.equal(status.auditOutcome, 'not_determined_by_runner');
    await assert.rejects(runAgent(options), /EEXIST/);
    writeFileSync(cliPath, '#!/usr/bin/env node\nconsole.log(JSON.stringify({type:"result",is_error:true,result:"failure"}));\n');
    assert.equal(await runAgent({...options,engine:'claude',outputDir:path.join(root,'failure')}), 1);
    writeFileSync(cliPath, '#!/usr/bin/env node\nconsole.log("not JSON");\n');
    assert.equal(await runAgent({...options,outputDir:path.join(root,'invalid')}), 1);
    writeFileSync(cliPath, '#!/usr/bin/env node\nsetInterval(()=>{},1000);\n');
    assert.equal(await runAgent({...options,outputDir:path.join(root,'timeout'),timeoutMs:100}), 1);
    assert.equal(JSON.parse(readFileSync(path.join(root,'timeout/process.json'))).timedOut,true);
    writeFileSync(cliPath, '#!/usr/bin/env node\nprocess.on("SIGTERM",()=>{});setInterval(()=>{},1000);\n');
    assert.equal(await runAgent({...options,outputDir:path.join(root,'stubborn'),timeoutMs:500}), 1);
    const stubborn=JSON.parse(readFileSync(path.join(root,'stubborn/process.json')));
    assert.equal(stubborn.timedOut,true);
    assert.equal(stubborn.signal,'SIGKILL');
    writeFileSync(cliPath, '#!/usr/bin/env node\nconsole.log(JSON.stringify({type:"turn.started"}));console.log(JSON.stringify({type:"error",message:"Reconnecting... 2/5 (Selected model is at capacity)"}));setInterval(()=>{},1000);\n');
    const startup = path.join(root, 'startup-stall');
    assert.equal(await runAgent({...options,outputDir:startup,startupTimeoutMs:500}), 1);
    const stalled = JSON.parse(readFileSync(path.join(startup, 'process.json')));
    assert.equal(stalled.startupTimedOut, true);
    assert.equal(stalled.timedOut, false);
    assert.match(readFileSync(path.join(startup, 'events.jsonl'), 'utf8'), /turn.started/);
    writeFileSync(cliPath, '#!/usr/bin/env node\nconsole.log(JSON.stringify({type:"assistant"}));setTimeout(()=>console.log(JSON.stringify({type:"result",result:"done"})),650);\n');
    assert.equal(await runAgent({...options,engine:'claude',outputDir:path.join(root,'started'),startupTimeoutMs:500}), 0);
  } finally { rmSync(root,{recursive:true,force:true}); }
});
