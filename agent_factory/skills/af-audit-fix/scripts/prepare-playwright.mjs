#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const guard = `
Audit requirements take precedence over generic test-healing advice:
- Capture a screenshot and trace for each audited case, including expected and actual outcomes.
- Preserve the original assertions and failure evidence. Never skip, mark fixme, weaken assertions,
  delete tests, or reinterpret an application defect as a passing test to achieve green results.
- Repair selectors or timing only when evidence proves the harness was wrong. Persist confirmed
  application failures in Praxis for local product fixes and a deployed re-audit.
- Use only the dedicated run-owned identities and fixtures. Do not modify unrelated user data.
`;

export function adaptDefinition(text, engine, nodeExecutable, playwrightCli) {
  // The upstream healer's final principles explicitly authorize suppressing persistent defects.
  text = text.replace(/^- If the error persists[^\n]*\n(?: {2}[^\n]*\n?){2}/m, '');
  if (engine === 'claude') {
    text = text.replace(/^model: [^\n]*\n/m, '');
    return text + '\n\n' + guard;
  }
  let instructions = 0;
  text = text.replace(/(developer_instructions = """)([\s\S]*?)(""")/, (_, start, body, end) => {
    instructions++;
    return start + body + '\n' + guard + end;
  });
  if (instructions !== 1 || !text.includes('[mcp_servers.playwright-test]')) throw new Error('Unexpected Codex agent format');
  let commands = 0, argumentsPatched = 0;
  text = text.replace(/^command = "npx"$/m, () => { commands++; return `command = ${JSON.stringify(nodeExecutable)}`; });
  text = text.replace(/^args = \["playwright", "run-test-mcp-server"\]$/m, () => {
    argumentsPatched++;
    return `args = ${JSON.stringify([playwrightCli, 'run-test-mcp-server', '--headless'])}`;
  });
  if (commands !== 1 || argumentsPatched !== 1) throw new Error('Unexpected Codex MCP format');
  return text;
}

export function prepare({engine, workspace, playwrightCli, expectedVersion}) {
  if (!['codex','claude'].includes(engine)) throw new Error('engine must be codex or claude');
  if (!workspace || !playwrightCli || !expectedVersion) throw new Error('workspace, playwright-cli and expected-version required');
  playwrightCli = realpathSync(playwrightCli);
  const version = JSON.parse(readFileSync(path.join(path.dirname(playwrightCli),'package.json'),'utf8')).version;
  if (version !== expectedVersion) throw new Error(`Playwright version ${version} differs from pinned ${expectedVersion}`);
  workspace = path.resolve(workspace);
  const agentFolder = path.join(workspace, engine === 'codex' ? '.codex/agents' : '.claude/agents');
  const names = ['planner','generator','healer'].map(role => engine === 'codex' ? `playwright_test_${role}.toml` : `playwright-test-${role}.md`);
  // A dedicated audit workspace can contain source. Do not overwrite existing seed/config/agents.
  for (const owned of ['seed.spec.ts','specs/README.md',...names.map(name=>path.relative(workspace,path.join(agentFolder,name))), ...(engine === 'claude' ? ['.mcp.json'] : [])]) {
    if (existsSync(path.join(workspace,owned))) throw new Error(`Refusing to overwrite ${owned}; use a fresh job-owned workspace or review a manual merge`);
  }
  mkdirSync(workspace,{recursive:true});
  const result = spawnSync(process.execPath,[playwrightCli,'init-agents',`--loop=${engine}`],{cwd:workspace,encoding:'utf8',shell:false});
  if (result.error || result.status !== 0) throw new Error(`Playwright initialization failed: ${result.error?.message ?? result.stderr}`);
  const definitions = names.map(name=>path.join(agentFolder,name));
  for (const filename of definitions) {
    const adapted = adaptDefinition(readFileSync(filename,'utf8'),engine,process.execPath,playwrightCli);
    writeFileSync(filename,adapted,{mode:0o600});
  }
  if (engine === 'claude') {
    const configPath=path.join(workspace,'.mcp.json');
    const config=JSON.parse(readFileSync(configPath,'utf8'));
    if (!config.mcpServers?.['playwright-test']) throw new Error('Generated Claude MCP server missing');
    config.mcpServers['playwright-test']={command:process.execPath,args:[playwrightCli,'run-test-mcp-server','--headless']};
    writeFileSync(configPath,JSON.stringify(config,null,2)+'\n',{mode:0o600});
  }
  return {engine,version,workspace,definitions,seed:path.join(workspace,'seed.spec.ts'),prerequisites:['Install matching @playwright/test in the job workspace','Install a supported browser and its OS libraries','Bind the seed to run-owned authentication and target URL before planner_setup_page','Trust the reviewed project MCP server configuration in the selected CLI']};
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args=process.argv.slice(2), options={};
    const names={'--engine':'engine','--workspace':'workspace','--playwright-cli':'playwrightCli','--expected-version':'expectedVersion'};
    for(let i=0;i<args.length;i+=2){if(!names[args[i]]||args[i+1]===undefined)throw new Error(`Unknown or missing argument ${args[i]}`);options[names[args[i]]]=args[i+1];}
    process.stdout.write(JSON.stringify(prepare(options),null,2)+'\n');
  } catch(error){process.stderr.write(error.message+'\n');process.exitCode=1;}
}
