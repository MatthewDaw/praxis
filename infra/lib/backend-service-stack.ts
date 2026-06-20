import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as apprunner from 'aws-cdk-lib/aws-apprunner';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

export interface BackendServiceStackProps extends cdk.StackProps {
  /** Cognito user pool id. Defaults to the deployed pool `us-east-1_4nAMe6bPK`. */
  readonly cognitoUserPoolId?: string;
  /** Cognito app client id. Defaults to the deployed client `3ij653bq912pi4f17l5hn9iqqn`. */
  readonly cognitoClientId?: string;
  /** Cognito region. Defaults to `us-east-1`. */
  readonly cognitoRegion?: string;
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
      'us-east-1_4nAMe6bPK';
    const cognitoClientId =
      props.cognitoClientId ??
      this.node.tryGetContext('cognitoClientId') ??
      '3ij653bq912pi4f17l5hn9iqqn';
    const cognitoRegion =
      props.cognitoRegion ??
      this.node.tryGetContext('cognitoRegion') ??
      'us-east-1';

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
      'praxis/knowledge-graph/db',
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
              { name: 'COGNITO_USER_POOL_ID', value: cognitoUserPoolId },
              { name: 'COGNITO_CLIENT_ID', value: cognitoClientId },
              { name: 'COGNITO_REGION', value: cognitoRegion },
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

    new cdk.CfnOutput(this, 'ServiceUrl', {
      value: `https://${this.service.attrServiceUrl}`,
    });
  }
}
