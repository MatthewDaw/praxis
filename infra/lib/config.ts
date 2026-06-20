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

/** Postgres database name created on the KG instance. */
export const DB_NAME = 'praxis_kg';

/** Burstable Graviton class shared by the EC2 (Phoenix) and RDS (KG) instances. */
export const GRAVITON = ec2.InstanceClass.BURSTABLE4_GRAVITON;

/** Deployed Cognito identity the backend validates JWTs against. */
export const COGNITO = {
  userPoolId: 'us-east-1_4nAMe6bPK',
  clientId: '3ij653bq912pi4f17l5hn9iqqn',
  region: REGION,
  userPoolName: 'praxis-users',
};

/** Session-capture storage resource names. */
export const SESSION_SLICES = {
  bucketName: 'praxis-session-slices',
  insightsTableName: 'praxis-session-insights',
};
