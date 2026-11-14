import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export function collectOwnedDiagnostics(ts, program, owned) {
  const paths = new Set(owned.map(file => path.resolve(file)));
  const diagnostics = [];
  for (const file of paths) {
    const source = program.getSourceFile(file);
    if (!source) throw new Error(`Authored source missing from compiler program: ${file}`);
    // The public per-file API retains options/global/declaration diagnostics and
    // resolves imported types, without semantically checking unrelated sources.
    diagnostics.push(...ts.getPreEmitDiagnostics(program, source));
  }
  return ts.sortAndDeduplicateDiagnostics(diagnostics.filter(
    diagnostic => !diagnostic.file || paths.has(path.resolve(diagnostic.file.fileName)),
  ));
}

export function main(args = process.argv.slice(2), workspace = process.cwd()) {
  const index = args.indexOf('--base');
  if (index < 0 || !args[index + 1]) throw new Error('Expected --base <ticket-base-sha>');
  const git = parameters => execFileSync('git', parameters, { cwd: workspace, encoding: 'utf8' }).trim();
  const base = git(['rev-parse', '--verify', `${args[index + 1]}^{commit}`]);
  const head = git(['rev-parse', 'HEAD']);
  const root = git(['rev-parse', '--show-toplevel']);
  const ts = createRequire(path.join(workspace, 'package.json'))('typescript');
  const owned = git(['diff', '--name-only', base, head, '--']).split('\n')
    .filter(file => /\.[cm]?tsx?$/.test(file))
    .map(file => path.resolve(root, file)).filter(file => ts.sys.fileExists(file));
  if (!owned.length) throw new Error('No authored TypeScript sources to check');
  const groups = new Map();
  for (const file of owned) {
    const config = ts.findConfigFile(path.dirname(file), ts.sys.fileExists, 'tsconfig.json');
    if (!config) throw new Error(`No compiler configuration for authored source: ${file}`);
    if (!groups.has(config)) groups.set(config, []);
    groups.get(config).push(file);
  }
  const host = { getCurrentDirectory: () => workspace, getCanonicalFileName: file => file, getNewLine: () => '\n' };
  let errors = 0;
  console.log(`checker=af-check-ticket-types scope=authored-files base=${base} revision=${head} typescript=${ts.version}`);
  for (const [configPath, files] of groups) {
    const config = ts.readConfigFile(configPath, ts.sys.readFile);
    if (config.error) throw new Error(ts.formatDiagnostics([config.error], host));
    const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, path.dirname(configPath));
    if (parsed.errors.length) throw new Error(ts.formatDiagnostics(parsed.errors, host));
    const context = parsed.fileNames.filter(file => file.endsWith('.d.ts') || /(?:^|[/\\])[^/\\]*config\.ts$/.test(file));
    const program = ts.createProgram([...files, ...context], parsed.options);
    const diagnostics = collectOwnedDiagnostics(ts, program, files);
    if (diagnostics.length) console.error(ts.formatDiagnostics(diagnostics, host));
    errors += diagnostics.length;
    console.log(`${path.relative(workspace, path.dirname(configPath)) || '.'}: authored roots=${files.length}; authored/global errors=${diagnostics.length}; imported types resolved; unrelated diagnostics not graded`);
  }
  return errors ? 1 : 0;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = main();
}
