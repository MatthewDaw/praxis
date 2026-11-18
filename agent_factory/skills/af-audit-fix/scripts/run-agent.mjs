#!/usr/bin/env node
// Process adapter only. Audit cases, findings and phase transitions belong in Praxis.
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync, mkdirSync, openSync, closeSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export function invocation(engine, executable, cwd, permissions = 'default', platform = process.platform) {
  if (!['codex', 'claude'].includes(engine)) throw new Error('engine must be codex or claude');
  if (!['default', 'workspace-write', 'full-access'].includes(permissions)) throw new Error('invalid permissions');
  let command = executable;
  const prefix = [];
  if (platform === 'win32' && /\.(cmd|bat)$/i.test(command)) {
    // npm's Windows batch shim requires cmd.exe. Bypass it using the actual JS bin instead.
    const paths = path.win32;
    const relative = engine === 'codex' ? '@openai/codex/bin/codex.js' : '@anthropic-ai/claude-code/cli.js';
    const js = paths.join(paths.dirname(command), 'node_modules', ...relative.split('/'));
    if (!existsSync(js)) throw new Error(`No direct JavaScript entry point for ${engine}; supply its native executable`);
    command = process.execPath;
    prefix.push(js);
  }
  const args = engine === 'codex'
    ? ['exec', '--json', '--color', 'never', '--cd', cwd, '-c', 'approval_policy="never"']
    : ['--print', '--verbose', '--output-format', 'stream-json', '--permission-prompts', 'none'];
  if (engine === 'codex' && permissions !== 'default') {
    args.push('--sandbox', permissions === 'full-access' ? 'danger-full-access' : 'workspace-write');
  }
  if (engine === 'claude' && permissions !== 'default') {
    args.push('--permission-mode', permissions === 'full-access' ? 'bypassPermissions' : 'acceptEdits');
  }
  if (engine === 'codex') args.push('-');
  return { command, args: [...prefix, ...args], cwd, shell: false };
}

export function interpretEvent(engine, event) {
  if (engine === 'claude') {
    if (event.type === 'result') return { result: event.result ?? '', failed: event.is_error === true || (event.subtype && event.subtype !== 'success') };
  } else {
    if (event.type === 'error' && /^Reconnecting\.\.\. \d+\/\d+\b/.test(event.message ?? '')) return {};
    if (event.type === 'turn.failed' || event.type === 'error') return { failed: true };
    if (event.type === 'item.completed' && event.item?.type === 'agent_message') return { result: event.item.text ?? '' };
    if (event.type === 'turn.completed') return { completed: true };
  }
  return {};
}

function findExecutable(engine, supplied) {
  if (supplied) return path.resolve(supplied);
  const extensions = process.platform === 'win32' ? ['.exe', '.cmd', ''] : [''];
  for (const folder of (process.env.PATH ?? '').split(path.delimiter)) {
    for (const extension of extensions) {
      const candidate = path.join(folder, engine + extension);
      if (existsSync(candidate)) return candidate;
    }
  }
  throw new Error(`${engine} executable not found; use --cli-path`);
}

export async function runAgent(options) {
  const { engine, cwd, promptFile, outputDir, cliPath, permissions = 'default',
    timeoutMs = 0, startupTimeoutMs = 0, idleTimeoutMs = 0 } = options;
  if (!cwd || !promptFile || !outputDir) throw new Error('cwd, prompt-file and output-dir are required');
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 0) throw new Error('timeout-ms must be a nonnegative integer');
  if (!Number.isSafeInteger(startupTimeoutMs) || startupTimeoutMs < 0) throw new Error('startup-timeout-ms must be a nonnegative integer');
  if (!Number.isSafeInteger(idleTimeoutMs) || idleTimeoutMs < 0) throw new Error('idle-timeout-ms must be a nonnegative integer');
  const prompt = readFileSync(promptFile, 'utf8');
  if (!prompt.trim()) throw new Error('prompt-file is empty');
  const launch = invocation(engine, findExecutable(engine, cliPath), path.resolve(cwd), permissions);
  mkdirSync(outputDir, { recursive: true, mode: 0o700 });
  const files = Object.fromEntries(['events.jsonl', 'stderr.log', 'final.txt', 'process.json'].map(name => [name, path.join(outputDir, name)]));
  // Refuse accidental reuse/overwrite of an earlier attempt's evidence.
  const handles = [];
  try {
    for (const filename of Object.values(files)) handles.push(openSync(filename, 'wx', 0o600));
  } catch (error) {
    for (const handle of handles) closeSync(handle);
    throw error;
  }
  const startedAt = new Date().toISOString();
  let failed = false, completed = false, finalText = '', pending = '', parseErrors = 0, timedOut = false;
  let startupTimer, startupTimedOut = false, idleTimer, idleTimedOut = false;
  const startupEvents = new Set(engine === 'codex'
    ? ['item.started', 'item.completed', 'turn.completed', 'turn.failed']
    : ['assistant', 'user', 'result']);
  const pendingTools = new Set();
  const child = spawn(launch.command, launch.args, { cwd: launch.cwd, shell: false, windowsHide: true, detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'] });
  const status = extra => writeFileSync(handles[3], JSON.stringify({ engine, startedAt, pid: child.pid ?? null, auditOutcome: 'not_determined_by_runner', ...extra }, null, 2));
  status({ processState: 'running' });
  const consume = line => {
    if (!line.trim()) return;
    try {
      const event = JSON.parse(line);
      const result = interpretEvent(engine, event);
      if (startupEvents.has(event.type)) {
        clearTimeout(startupTimer);
        clearTimeout(idleTimer);
        if (engine === 'codex') {
          if (event.type === 'item.started' && event.item?.id) pendingTools.add(event.item.id);
          if (event.type === 'item.completed') pendingTools.delete(event.item?.id);
        } else if (Array.isArray(event.message?.content)) {
          for (const content of event.message.content) {
            if (content.type === 'tool_use' && content.id) pendingTools.add(content.id);
            if (content.type === 'tool_result') pendingTools.delete(content.tool_use_id);
          }
        }
        if (idleTimeoutMs && !pendingTools.size && !result.completed && !result.failed && event.type !== 'result') {
          idleTimer = setTimeout(() => { idleTimedOut = true; terminate(); }, idleTimeoutMs);
        }
      }
      failed ||= result.failed === true;
      completed ||= result.completed === true || (engine === 'claude' && result.result !== undefined);
      if (result.result !== undefined) finalText = result.result;
    } catch { parseErrors++; }
  };
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => {
    writeFileSync(handles[0], chunk);
    pending += chunk;
    let newline;
    while ((newline = pending.indexOf('\n')) !== -1) {
      consume(pending.slice(0, newline));
      pending = pending.slice(newline + 1);
    }
    if (pending.length > 16 * 1024 * 1024) { parseErrors++; pending = ''; }
  });
  child.stderr.on('data', chunk => writeFileSync(handles[1], chunk));
  child.stdin.on('error', () => {}); // A failed CLI can exit before accepting its prompt.
  child.stdin.end(prompt);
  let forceTimer;
  const terminate = () => {
    if (process.platform === 'win32') {
      if (child.pid) spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { shell: false, windowsHide: true, stdio: 'ignore' }).unref();
    } else {
      try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill('SIGTERM'); }
      if (!forceTimer) forceTimer = setTimeout(() => {
        try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
      }, 5000);
    }
  };
  let interrupted = false;
  const interrupt = () => { interrupted = true; terminate(); };
  process.once('SIGINT', interrupt);
  process.once('SIGTERM', interrupt);
  const timer = timeoutMs ? setTimeout(() => { timedOut = true; terminate(); }, timeoutMs) : null;
  startupTimer = startupTimeoutMs
    ? setTimeout(() => { startupTimedOut = true; terminate(); }, startupTimeoutMs)
    : null;
  const outcome = await new Promise(resolve => {
    child.once('error', error => resolve({ exitCode: null, signal: null, error: error.message }));
    child.once('close', (exitCode, signal) => resolve({ exitCode, signal }));
  });
  if (timer) clearTimeout(timer);
  if (startupTimer) clearTimeout(startupTimer);
  if (forceTimer) clearTimeout(forceTimer);
  process.removeListener('SIGINT', interrupt);
  process.removeListener('SIGTERM', interrupt);
  consume(pending);
  clearTimeout(idleTimer);
  const success = outcome.exitCode === 0 && !failed && completed && !parseErrors && !timedOut && !startupTimedOut && !idleTimedOut && !interrupted;
  writeFileSync(handles[2], finalText);
  // Truncate before rewriting the same descriptor (offset remains at the first write's end).
  closeSync(handles[3]);
  writeFileSync(files['process.json'], JSON.stringify({ engine, startedAt, finishedAt: new Date().toISOString(), pid: child.pid ?? null, processState: success ? 'exited' : 'failed', ...outcome, semanticError: failed, completedEvent: completed, parseErrors, timedOut, startupTimedOut, idleTimedOut, interrupted, auditOutcome: 'not_determined_by_runner' }, null, 2), { mode: 0o600 });
  for (const handle of handles.slice(0, 3)) closeSync(handle);
  return success ? 0 : 1;
}

function parseArgs(argv) {
  const options = {};
  const names = { '--engine': 'engine', '--cwd': 'cwd', '--prompt-file': 'promptFile', '--output-dir': 'outputDir', '--cli-path': 'cliPath', '--permissions': 'permissions', '--timeout-ms': 'timeoutMs', '--startup-timeout-ms': 'startupTimeoutMs', '--idle-timeout-ms': 'idleTimeoutMs' };
  for (let index = 0; index < argv.length; index += 2) {
    if (!names[argv[index]] || argv[index + 1] === undefined) throw new Error(`Unknown or missing option: ${argv[index]}`);
    options[names[argv[index]]] = argv[index + 1];
  }
  if (options.timeoutMs !== undefined) options.timeoutMs = Number(options.timeoutMs);
  if (options.startupTimeoutMs !== undefined) options.startupTimeoutMs = Number(options.startupTimeoutMs);
  if (options.idleTimeoutMs !== undefined) options.idleTimeoutMs = Number(options.idleTimeoutMs);
  return options;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await runAgent(parseArgs(process.argv.slice(2))); }
  catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1; }
}
