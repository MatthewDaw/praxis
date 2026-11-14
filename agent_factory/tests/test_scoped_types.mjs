import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { collectOwnedDiagnostics } from '../scripts/af-check-ticket-types.mjs';

const require = createRequire(import.meta.url);
const ts = require(process.env.MVPVUE_TYPESCRIPT_MODULE || 'typescript');

function fixture(body, options = {}) {
  const root = mkdtempSync(path.join(tmpdir(), 'factory-owned-types-'));
  const owned = path.join(root, 'owned.ts');
  writeFileSync(path.join(root, 'dependency.ts'), 'export function count(value: string): number { return value.length; }\nexport const legacy: number = "bad";\n');
  writeFileSync(owned, body);
  const program = ts.createProgram([owned], {
    strict: true, noEmit: true, target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS, lib: ['lib.es5.d.ts'], ...options,
  });
  const summarize = diagnostics => diagnostics.map(d => ({
    code: d.code, file: d.file?.fileName, start: d.start,
    message: ts.flattenDiagnosticMessageText(d.messageText, '\n'),
  }));
  try {
    const all = ts.getPreEmitDiagnostics(program);
    const expected = all.filter(d => !d.file || d.file.fileName === owned);
    const actual = collectOwnedDiagnostics(ts, program, [owned]);
    assert.deepEqual(summarize(actual), summarize(ts.sortAndDeduplicateDiagnostics(expected)));
    return { all, actual };
  } finally { rmSync(root, { recursive: true, force: true }); }
}

test('unrelated dependency diagnostics are not graded, matching the original boundary', () => {
  const { all, actual } = fixture('import { count } from "./dependency"; export const value: number = count("ok");');
  assert.ok(all.some(d => d.file?.fileName.endsWith('dependency.ts')));
  assert.equal(actual.length, 0);
});

test('imported signatures and authored syntax errors are still checked', () => {
  assert.ok(fixture('import { count } from "./dependency"; export const value = count(42);').actual.some(d => d.code === 2345));
  assert.ok(fixture('export const value: number = ;').actual.length > 0);
});

test('compiler option errors still fail', () => {
  assert.ok(fixture('export const value = 1;', {
    moduleResolution: ts.ModuleResolutionKind.NodeNext, module: ts.ModuleKind.CommonJS,
  }).actual.length > 0);
});

test('missing authored roots fail instead of falling back to a whole-program check', () => {
  const program = ts.createProgram([], {});
  assert.throws(() => collectOwnedDiagnostics(ts, program, ['/missing/owned.ts']), /missing from compiler program/);
});
