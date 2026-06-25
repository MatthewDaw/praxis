import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as apprunner from 'aws-cdk-lib/aws-apprunner';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';
import { COGNITO, DB_SECRET_NAME } from './config';

/**
 * Edge-layer flood ceiling: max requests per source IP over WAF's fixed 5-minute
 * window before the rate-based rule blocks that IP. A blunt anti-flood ceiling set
 * well above any legitimate single-client rate — the fine-grained per-principal
 * throttle is the app-layer limiter (knowledge/serve/rate_limit.py).
 */
const WAF_IP_RATE_LIMIT_PER_5MIN = 3000;

export interface BackendServiceStackProps extends cdk.StackProps {
  /** Cognito user pool id. Defaults to the deployed pool `us-east-1_4nAMe6bPK`. */
  readonly cognitoUserPoolId?: string;
  /** Cognito app client id. Defaults to the deployed client `3ij653bq912pi4f17l5hn9iqqn`. */
  readonly cognitoClientId?: string;
  /** Cognito region. Defaults to `us-east-1`. */
  readonly cognitoRegion?: string;
  /** OpenRouter API key. Defaults to the `OPENROUTER_API_KEY` env var / `openrouterApiKey` context. */
  readonly openrouterApiKey?: string;
}

/**
 * The PRAXIS backend on AWS App Runner: a managed container that serves the
 * FastAPI app (`knowledge.serve`) on a free `*.awsapprunner.com` HTTPS URL.
 *
 * CDK builds the repo-root `Dockerfile` into the CDK assets ECR repo
 * (`DockerImageAsset`), and App Runner pulls it via the access role. The
 * instance role can read the RDS master secret (`praxis/knowledge-graph/db`)
 * so the app resolves its Postgres connection at runtime from Secrets Manager.
 *
 * The service reaches the existing public RDS endpoint + Cognito over the
 * internet (no VPC connector); RDS is already open on 5432 and gated by the
 * Secrets-Manager credentials.
 */
export class BackendServiceStack extends cdk.Stack {
  public readonly service: apprunner.CfnService;

  constructor(scope: Construct, id: string, props: BackendServiceStackProps = {}) {
    super(scope, id, props);

    const cognitoUserPoolId =
      props.cognitoUserPoolId ??
      this.node.tryGetContext('cognitoUserPoolId') ??
      COGNITO.userPoolId;
    const cognitoClientId =
      props.cognitoClientId ??
      this.node.tryGetContext('cognitoClientId') ??
      COGNITO.clientId;
    const cognitoRegion =
      props.cognitoRegion ??
      this.node.tryGetContext('cognitoRegion') ??
      COGNITO.region;

    // OpenRouter key for the runtime embed/judge/distillation paths. Injected at
    // deploy time from the `OPENROUTER_API_KEY` env var (fed by a GitHub secret in
    // the deploy workflow); omitted from the service when unset so a local `cdk
    // deploy` without it doesn't push an empty value.
    const openrouterApiKey =
      props.openrouterApiKey ??
      this.node.tryGetContext('openrouterApiKey') ??
      process.env.OPENROUTER_API_KEY;

    // Build the backend image from the repo-root Dockerfile and publish it to
    // the CDK assets ECR repo.
    const asset = new ecrAssets.DockerImageAsset(this, 'BackendImage', {
      directory: path.join(__dirname, '../..'),
    });

    // App Runner pulls the image from ECR using this access role.
    const accessRole = new iam.Role(this, 'AccessRole', {
      assumedBy: new iam.ServicePrincipal('build.apprunner.amazonaws.com'),
    });
    asset.repository.grantPull(accessRole);

    // The running task assumes this role; it can read the RDS master secret.
    const instanceRole = new iam.Role(this, 'InstanceRole', {
      assumedBy: new iam.ServicePrincipal('tasks.apprunner.amazonaws.com'),
    });
    secretsmanager.Secret.fromSecretNameV2(
      this,
      'DbSecret',
      DB_SECRET_NAME,
    ).grantRead(instanceRole);

    this.service = new apprunner.CfnService(this, 'Service', {
      sourceConfiguration: {
        autoDeploymentsEnabled: false,
        authenticationConfiguration: {
          accessRoleArn: accessRole.roleArn,
        },
        imageRepository: {
          imageIdentifier: asset.imageUri,
          imageRepositoryType: 'ECR',
          imageConfiguration: {
            port: '8080',
            runtimeEnvironmentVariables: [
              { name: 'PRAXIS_API_HOST', value: '0.0.0.0' },
              { name: 'AWS_REGION', value: this.region },
              // Opt in to the Secrets Manager DSN fallback. db.py only resolves a
              // remote DSN when this is set, so a local script that forgets to
              // load .env can never silently reach this production database.
              { name: 'PRAXIS_DB_ALLOW_REMOTE', value: '1' },
              { name: 'COGNITO_USER_POOL_ID', value: cognitoUserPoolId },
              { name: 'COGNITO_CLIENT_ID', value: cognitoClientId },
              { name: 'COGNITO_REGION', value: cognitoRegion },
              ...(openrouterApiKey
                ? [{ name: 'OPENROUTER_API_KEY', value: openrouterApiKey }]
                : []),
            ],
          },
        },
      },
      instanceConfiguration: {
        cpu: '0.25 vCPU',
        memory: '0.5 GB',
        instanceRoleArn: instanceRole.roleArn,
      },
      healthCheckConfiguration: {
        path: '/health',
        protocol: 'HTTP',
      },
    });

    // Edge-layer WAF: defense-in-depth complementing the app-layer per-principal
    // limiter. App Runner enforces no per-IP flood ceiling on its own and the app
    // limiter buckets per authenticated principal, so an unauthenticated flood (no
    // valid principal to bucket on) could still hammer the service. A WAFv2
    // rate-based rule blocks any single source IP that exceeds the 5-minute ceiling
    // before the request reaches the container.
    const webAcl = new wafv2.CfnWebACL(this, 'BackendWebAcl', {
      scope: 'REGIONAL', // App Runner is a regional resource (not CloudFront).
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'PraxisBackendWebAcl',
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: 'PerIpRateLimit',
          priority: 0,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: WAF_IP_RATE_LIMIT_PER_5MIN,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'PerIpRateLimit',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    new wafv2.CfnWebACLAssociation(this, 'BackendWebAclAssociation', {
      resourceArn: this.service.attrServiceArn,
      webAclArn: webAcl.attrArn,
    });

    new cdk.CfnOutput(this, 'ServiceUrl', {
      value: `https://${this.service.attrServiceUrl}`,
    });
  }
}
