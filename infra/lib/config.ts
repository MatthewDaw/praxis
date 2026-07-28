import * as cdk from 'aws-cdk-lib/core';
import * as ec2 from 'aws-cdk-lib/aws-ec2';

/**
 * Single source of truth for cross-stack PRAXIS infra constants. Stacks read
 * their defaults from here instead of redeclaring literals, so a value used in
 * more than one place — the DB secret name, the deployed Cognito pool, the
 * open-by-default CIDR — is defined exactly once.
 *
 * Per-deploy overrides still flow through CDK context (`-c key=value`) at the
 * `bin/app.ts` layer; these are only the baked-in defaults.
 */

/** Region the whole PRAXIS stack is colocated in. */
export const REGION = process.env.CDK_DEFAULT_REGION ?? 'us-east-1';

/**
 * Resolved CDK environment shared by every stack. Account is left undefined
 * when `CDK_DEFAULT_ACCOUNT` is unset so `cdk synth` works with no creds.
 */
export const ENV: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: REGION,
};

/**
 * Open-by-default ingress CIDR. A fresh deploy is reachable; lock it down
 * per-stack via context (`-c allowedCidr=1.2.3.4/32`). Resources behind it
 * still require their own credentials (RDS secret, Phoenix auth).
 */
export const DEFAULT_ALLOWED_CIDR = '0.0.0.0/0';

/** Secrets Manager name holding the RDS master credentials. */
export const DB_SECRET_NAME = 'praxis/knowledge-graph/db';

/**
 * Secrets Manager name holding the GitHub personal access token (R1) used by
 * the productivity backend. The secret resource itself
 * (`AWS::SecretsManager::Secret`) is created fresh in
 * `backend-service-stack.ts`; this constant is only the shared name so the
 * runtime and the stack agree on it without duplicating the literal.
 */
export const GITHUB_TOKEN_SECRET_NAME = 'praxis/github/token';

/**
 * Secrets Manager name holding the OpenRouter API key used by the runtime
 * embed/judge/distillation paths. Like the GitHub token above, the secret
 * resource itself is created in `backend-service-stack.ts`; this constant is
 * only the shared name.
 *
 * The key reaches the container through App Runner's `RuntimeEnvironmentSecrets`
 * (an ARN reference App Runner resolves at launch), NOT `RuntimeEnvironmentVariables`
 * — the latter is echoed verbatim by `apprunner describe-service` and the console,
 * which is exactly how this key sat readable in plaintext to anyone holding
 * `apprunner:DescribeService` until 2026-07-28.
 */
export const OPENROUTER_API_KEY_SECRET_NAME = 'praxis/openrouter/api-key';

/** Postgres database name created on the KG instance. */
export const DB_NAME = 'praxis_kg';

/** Burstable Graviton class shared by the EC2 (Phoenix) and RDS (KG) instances. */
export const GRAVITON = ec2.InstanceClass.BURSTABLE4_GRAVITON;

/** Deployed Cognito identity the backend validates JWTs against.
 *  This is the pool AuthUserPoolStack creates in the deploy account
 *  (sotos / 528782700781, us-east-1). */
export const COGNITO = {
  userPoolId: 'us-east-1_dqDCickOP',
  clientId: '3v12800pitg5l6tm4er2qati8g',
  region: REGION,
  userPoolName: 'praxis-users',
};

/** Session-capture storage resource names. */
export const SESSION_SLICES = {
  bucketName: 'praxis-session-slices',
  insightsTableName: 'praxis-session-insights',
};
