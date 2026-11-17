# Browser workflow and upstream provenance

Use these complementary primary sources:

- [Anthropic webapp-testing](https://github.com/anthropics/skills/tree/e4bb5d960d87126ff3b3a8af1a104b7db67560ba/skills/webapp-testing),
  reviewed at commit `e4bb5d960d87126ff3b3a8af1a104b7db67560ba`.
  Read its `SKILL.md` for reconnaissance, rendered control discovery, screenshots
  and console capture. Its Python examples are useful for exploration; the
  repeatable regression suite below uses the project's Playwright Test toolchain.
- [Microsoft Playwright Test Agents](https://playwright.dev/docs/test-agents),
  planner → generator → healer. Pin the project's existing Playwright version
  rather than fetching an unreviewed latest version for every audit.

In a fresh job-owned workspace, prepare the selected engine using the installed,
pinned Playwright CLI. This helper does not install packages or fetch latest:

```sh
node scripts/prepare-playwright.mjs --engine codex --workspace /private/audit-job \
  --playwright-cli /project/node_modules/playwright/cli.js --expected-version 1.65.1
# Use --engine claude and a separate workspace when selecting Claude.
```

The helper calls `init-agents --loop=<engine>` directly. Codex receives native
`.codex/agents/playwright_test_{planner,generator,healer}.toml` definitions with
per-agent MCP configuration; Claude receives `.claude/agents/playwright-test-*.md`
and `.mcp.json`. It removes the upstream Claude model override, pins the MCP server
to the same Node/Playwright CLI with `--headless`, removes advice to suppress
persistent defects, and adds this audit's evidence and assertion requirements.
It refuses to overwrite preexisting seed, agent or MCP files; use a fresh workspace
or review a manual merge. Read the
generated definitions and use their planner, generator and healer roles. If the
CLI/version lacks these definitions, report that dependency failure instead of
claiming the upstream agents ran. Native subagent delegation is optional when
unavailable; explicitly execute each role's instructions serially and record this.

Install matching `@playwright/test` in the job workspace, a compatible browser and
its OS libraries. The generated `seed.spec.ts` starts empty: bind it to the deployed
target and run-owned authentication before `planner_setup_page`. Ensure the test
config discovers that seed, and approve the reviewed MCP configuration through the
engine's normal trust mechanism. A private browser identity per role is required.
Test the MCP initialize/tools-list handshake before claiming the agents are ready:
`planner_setup_page`, `planner_save_plan`, `generator_setup_page`,
`generator_write_test`, `test_run`, `test_debug`, `browser_snapshot`, and
`browser_take_screenshot` must be available. Playwright 1.65.1 was verified with
both engines' generated definitions and its real MCP server (92 tools); that
handshake alone is infrastructure evidence, not proof any application case passed.

Initialize and verify this tooling once per workspace and version. A normal page sweep uses direct Playwright clicks and observed outcomes in a shared browser context. Reserve generated regression runs and healer reviews for failures and complex workflows.

The planner interacts with the real deployed app using dedicated test identities
and writes a scenario plan with observable outcomes. The generator verifies the
selectors live, creates executable tests from that plan and runs them. The healer
triages failures using trace/DOM/network evidence, distinguishing harness errors
from application defects. Keep original failure artifacts and expectations.

For each browser session attach console, pageerror, requestfailed and relevant
response listeners before navigation; use a fresh browser context per identity.
Share evidence across a page visit. Capture a screenshot or trace when a failure, visual assertion or complex workflow needs it. Inspect rendered
controls after application readiness; select by role/label where possible. For
long polling or SSE use a visible ready-state assertion rather than relying only
on networkidle. Test actions, resulting state and reload persistence. API setup
and verification can assist but cannot substitute for the requested UI audit.

Store sanitized evidence under run/case/build paths outside disposable checkouts.
Do not publish tokens, headers, private forms or unredacted traces. Preserve private
raw evidence separately when redaction would destroy diagnostic value. Stop the
browser and clean up owned fixtures on success or failure. Do not import a real
user's signed-in browser profile.

For known controls, import `runPageSweep` from `../scripts/page-sweep.mjs` in
a private batch script and submit the clicks and their assertions together. This
avoids a model turn, subprocess and fixed sleep between each known interaction.
Pass the installed Playwright `chromium`, the target `origin`, `contextOptions`
with the prepared profile, observed build, role and a fresh private output path.
Use ordinary Playwright locators and assertions inside each check. For example:

```js
await runPageSweep(options, async visit => {
  await visit('/settings', async (page, check) => {
    await check('open-preferences', async () => {
      await page.getByRole('button', { name: 'Preferences' }).click();
      await page.getByRole('dialog', { name: 'Preferences' }).waitFor();
    });
  });
});
```

The helper shares a browser context across visits, records timing and keeps
partial observations if an assertion throws. A missed selector calls for
triage. Reuse the log for each applicable result. Only record a scenario verdict
after its expected behavior has been verified. Use interactive exploration when
a page reveals an unfamiliar control.
