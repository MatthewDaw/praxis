#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { ENV } from '../lib/config';
import { NetworkStack } from '../lib/network-stack';
import { SessionSlicesStack } from '../lib/session-slices-stack';
import { AuthUserPoolStack } from '../lib/auth-user-pool-stack';
import { KnowledgeGraphDbStack } from '../lib/knowledge-graph-db-stack';
import { PhoenixStack } from '../lib/phoenix-stack';
import { BackendServiceStack } from '../lib/backend-service-stack';
import { FrontendSiteStack } from '../lib/frontend-site-stack';
import { DnsStack } from '../lib/dns-stack';

const app = new cdk.App();

// Deploys to the account from the ambient CDK CLI credentials, us-east-1 to stay
// colocated with the rest of the PRAXIS stack (see lib/config.ts). Account is
// left undefined at synth time when CDK_DEFAULT_ACCOUNT is unset, so `cdk synth`
// works with no creds.
const env = ENV;

// Shared VPC consumed by the VPC-bound stacks (Phoenix, KG DB). Declared first
// so the cross-stack reference makes those stacks deploy after it. Per-stack
// physical-name / CIDR defaults now live in lib/config.ts; pass `-c key=value`
// to override.
const network = new NetworkStack(app, 'PraxisNetworkStack', { env });

new SessionSlicesStack(app, 'PraxisSessionSlicesStack', {
  env,
  bucketName: app.node.tryGetContext('sliceBucketName'),
  insightsTableName: app.node.tryGetContext('insightsTableName'),
  sliceRetentionDays: app.node.tryGetContext('sliceRetentionDays'),
});

new AuthUserPoolStack(app, 'PraxisAuthUserPoolStack', {
  env,
  userPoolName: app.node.tryGetContext('authUserPoolName'),
});

new KnowledgeGraphDbStack(app, 'PraxisKnowledgeGraphDbStack', {
  env,
  vpc: network.vpc,
  databaseName: app.node.tryGetContext('databaseName'),
  allowedCidr: app.node.tryGetContext('allowedCidr'),
});

const dns = new DnsStack(app, 'PraxisDnsStack', {
  env,
  domainName: app.node.tryGetContext('domainName') ?? 'praxiskg.com',
});

new PhoenixStack(app, 'PraxisPhoenixStack', {
  env,
  vpc: network.vpc,
  imageTag: app.node.tryGetContext('phoenixImageTag'),
  domain: app.node.tryGetContext('phoenixDomain') ?? 'phoenix.praxiskg.com',
  hostedZone: dns.zone,
  allowedWebCidr: app.node.tryGetContext('phoenixAllowedWebCidr'),
  dataVolumeGib: app.node.tryGetContext('phoenixDataVolumeGib'),
});

new BackendServiceStack(app, 'PraxisBackendServiceStack', { env });

new FrontendSiteStack(app, 'PraxisFrontendSiteStack', { env });
