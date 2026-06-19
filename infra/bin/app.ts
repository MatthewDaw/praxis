#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { SessionsTableStack } from '../lib/sessions-table-stack';
import { KnowledgeGraphDbStack } from '../lib/knowledge-graph-db-stack';

const app = new cdk.App();

// Deploys to the account from the ambient CDK CLI credentials, us-east-1 to stay
// colocated with the rest of the PRAXIS stack. Account is left undefined at synth
// time when CDK_DEFAULT_ACCOUNT is unset, so `cdk synth` works with no creds.
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

new SessionsTableStack(app, 'PraxisSessionsTableStack', {
  env,
  tableName: app.node.tryGetContext('tableName') ?? 'praxis-sessions',
});

new KnowledgeGraphDbStack(app, 'PraxisKnowledgeGraphDbStack', {
  env,
  databaseName: app.node.tryGetContext('databaseName') ?? 'praxis_kg',
  allowedCidr: app.node.tryGetContext('allowedCidr'),
});
