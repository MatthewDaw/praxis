import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { prepare, adaptDefinition } from './prepare-playwright.mjs';

test('upstream Claude model and failure suppression are removed; evidence and original assertions retained',()=>{
  const text='---\nmodel: sonnet\n---\n- If the error persists do not fail\n  skip it\n  and leave a comment\n- Keep exploring\n';
  const adapted=adaptDefinition(text,'claude','/node','/cli');
  assert.equal(adapted.includes('model: sonnet'),false);
  assert.equal(adapted.includes('If the error persists'),false);
  assert.ok(adapted.includes('Never skip'));
  assert.ok(adapted.includes('screenshot and trace'));
  assert.ok(adapted.includes('Keep exploring'));
});

test('real pinned Playwright generates safely adapted native definitions for each engine', {skip: !process.env.AF_AUDIT_TEST_PLAYWRIGHT_CLI},()=>{
  const root=mkdtempSync(path.join(tmpdir(),'audit-pw-prepare-'));
  try{
    const playwrightCli=process.env.AF_AUDIT_TEST_PLAYWRIGHT_CLI;
    const expectedVersion=JSON.parse(readFileSync(path.join(path.dirname(playwrightCli),'package.json'))).version;
    assert.throws(()=>prepare({engine:'codex',workspace:path.join(root,'wrong'),playwrightCli,expectedVersion:'0.0.0'}),/differs/);
    for(const engine of ['codex','claude']){
      const workspace=path.join(root,engine);
      const result=prepare({engine,workspace,playwrightCli,expectedVersion});
      assert.equal(result.definitions.length,3);
      for(const filename of result.definitions){
        const text=readFileSync(filename,'utf8');
        assert.equal(text.includes('If the error persists'),false);
        assert.equal(/^model: /m.test(text),false);
        assert.ok(text.includes('Never skip'));
        if(engine==='codex'){
          assert.ok(text.includes(JSON.stringify(process.execPath)));
          assert.ok(text.includes('--headless'));
          assert.equal(text.includes('command = "npx"'),false);
        }
      }
      if(engine==='claude'){
        const mcp=JSON.parse(readFileSync(path.join(workspace,'.mcp.json'))).mcpServers['playwright-test'];
        assert.equal(mcp.command,process.execPath);
        assert.deepEqual(mcp.args,[playwrightCli,'run-test-mcp-server','--headless']);
      }
      const marker=path.join(workspace,'seed.spec.ts');
      writeFileSync(marker,'existing project content');
      assert.throws(()=>prepare({engine,workspace,playwrightCli,expectedVersion}),/Refusing to overwrite/);
      assert.equal(readFileSync(marker,'utf8'),'existing project content');
    }
  }finally{rmSync(root,{recursive:true,force:true});}
});
