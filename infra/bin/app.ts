#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { SessionSlicesStack } from '../lib/session-slices-stack';
import { AuthUserPoolStack } from '../lib/auth-user-pool-stack';
import { KnowledgeGraphDbStack } from '../lib/knowledge-graph-db-stack';
import { PhoenixStack } from '../lib/phoenix-stack';
import { BackendServiceStack } from '../lib/backend-service-stack';
import { FrontendSiteStack } from '../lib/frontend-site-stack';

const app = new cdk.App();

// Deploys to the account from the ambient CDK CLI credentials, us-east-1 to stay
// colocated with the rest of the PRAXIS stack. Account is left undefined at synth
// time when CDK_DEFAULT_ACCOUNT is unset, so `cdk synth` works with no creds.
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

new SessionSlicesStack(app, 'PraxisSessionSlicesStack', {
  env,
  bucketName: app.node.tryGetContext('sliceBucketName') ?? 'praxis-session-slices',
  insightsTableName: app.node.tryGetContext('insightsTableName') ?? 'praxis-session-insights',
  sliceRetentionDays: app.node.tryGetContext('sliceRetentionDays'),
});

new AuthUserPoolStack(app, 'PraxisAuthUserPoolStack', {
  env,
  userPoolName: app.node.tryGetContext('authUserPoolName') ?? 'praxis-users',
});

new KnowledgeGraphDbStack(app, 'PraxisKnowledgeGraphDbStack', {
  env,
  databaseName: app.node.tryGetContext('databaseName') ?? 'praxis_kg',
  allowedCidr: app.node.tryGetContext('allowedCidr'),
});

new PhoenixStack(app, 'PraxisPhoenixStack', {
  env,
  imageTag: app.node.tryGetContext('phoenixImageTag'),
  domain: app.node.tryGetContext('phoenixDomain'),
  allowedWebCidr: app.node.tryGetContext('phoenixAllowedWebCidr'),
  dataVolumeGib: app.node.tryGetContext('phoenixDataVolumeGib'),
});

new BackendServiceStack(app, 'PraxisBackendServiceStack', { env });

new FrontendSiteStack(app, 'PraxisFrontendSiteStack', { env });
