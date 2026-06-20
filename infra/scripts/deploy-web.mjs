#!/usr/bin/env node
// Orchestrates the website deploy so the frontend bundle is built with the real
// backend URL baked in (avoids a backend<->frontend circular dependency):
//   1. Deploy the App Runner backend, capturing its ServiceUrl.
//   2. Build the React app pointed at that URL (+ known Cognito values).
//   3. Deploy the S3/CloudFront frontend, uploading the fresh dist/.
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import * as path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const infraDir = path.resolve(__dirname, '..');
const frontendDir = path.resolve(infraDir, '../frontend-react');
const outputsFile = path.join(infraDir, 'cdk-web-out.json');

// Known/deployed Cognito values (see infra/README.md, plan PINNED NAMES).
const COGNITO_USER_POOL_ID = 'us-east-1_4nAMe6bPK';
const COGNITO_CLIENT_ID = '3ij653bq912pi4f17l5hn9iqqn';
const COGNITO_REGION = 'us-east-1';

function run(command, args, opts = {}) {
  const printable = [command, ...args].join(' ');
  console.log(`\n> ${printable}\n`);
  const result = spawnSync(command, args, {
    stdio: 'inherit',
    shell: process.platform === 'win32',
    ...opts,
  });
  if (result.status !== 0) {
    console.error(`\nCommand failed (exit ${result.status}): ${printable}`);
    process.exit(result.status ?? 1);
  }
}

// 1. Deploy the backend and capture outputs.
run('npx', [
  'cdk',
  'deploy',
  'PraxisBackendServiceStack',
  '--require-approval',
  'never',
  '--outputs-file',
  outputsFile,
], { cwd: infraDir });

// 2. Extract the App Runner service URL.
const outputs = JSON.parse(readFileSync(outputsFile, 'utf8'));
const serviceUrl = outputs?.PraxisBackendServiceStack?.ServiceUrl;
if (!serviceUrl) {
  console.error(`Could not find PraxisBackendServiceStack.ServiceUrl in ${outputsFile}`);
  process.exit(1);
}
console.log(`\nBackend ServiceUrl: ${serviceUrl}`);

// 3. Build the frontend with the real backend URL + Cognito values baked in.
const buildEnv = {
  ...process.env,
  VITE_PRAXIS_API_BASE_URL: serviceUrl,
  VITE_PRAXIS_POSTGRES_API_BASE_URL: serviceUrl,
  VITE_COGNITO_USER_POOL_ID: COGNITO_USER_POOL_ID,
  VITE_COGNITO_CLIENT_ID: COGNITO_CLIENT_ID,
  VITE_COGNITO_REGION: COGNITO_REGION,
};
run('npm', ['ci'], { cwd: frontendDir, env: buildEnv });
run('npm', ['run', 'build'], { cwd: frontendDir, env: buildEnv });

// 4. Deploy the frontend (uploads the fresh dist/ and invalidates CloudFront).
run('npx', [
  'cdk',
  'deploy',
  'PraxisFrontendSiteStack',
  '--require-approval',
  'never',
  '--outputs-file',
  outputsFile,
], { cwd: infraDir });

const frontendOutputs = JSON.parse(readFileSync(outputsFile, 'utf8'));
const distributionUrl = frontendOutputs?.PraxisFrontendSiteStack?.DistributionUrl;
console.log('\nWebsite deploy complete.');
console.log(`Backend:  ${serviceUrl}`);
console.log(`Frontend: ${distributionUrl ?? '(see PraxisFrontendSiteStack outputs)'}`);
