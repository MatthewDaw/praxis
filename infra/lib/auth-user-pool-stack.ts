import * as cdk from 'aws-cdk-lib/core';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import { COGNITO } from './config';

export interface AuthUserPoolStackProps extends cdk.StackProps {
  /** Physical Cognito User Pool name. Defaults to `praxis-users`. */
  readonly userPoolName?: string;
}

/**
 * The PRAXIS authentication store: an AWS Cognito User Pool gating the dashboard.
 *
 * Email + password with self-signup, so visitors can register, confirm via an
 * emailed code, and log in. The user's immutable `sub` (UUID) becomes the
 * app-level `user_id`; orgs are an app-level concept layered on top in the
 * backend (not Cognito groups).
 *
 * A single public SPA client (no secret) is provisioned for the React app's
 * Amplify `<Authenticator>`, which uses SRP — no Hosted UI / OAuth callbacks
 * are needed. `removalPolicy: RETAIN` keeps real user accounts alive across
 * stack churn.
 */
export class AuthUserPoolStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: AuthUserPoolStackProps = {}) {
    super(scope, id, props);

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: props.userPoolName ?? COGNITO.userPoolName,
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Public SPA client: no secret, SRP auth flow (Amplify), and existence
    // errors masked so login/reset don't leak which emails are registered.
    this.userPoolClient = this.userPool.addClient('SpaClient', {
      generateSecret: false,
      authFlows: { userSrp: true },
      preventUserExistenceErrors: true,
    });

    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, 'UserPoolRegion', { value: this.region });
    new cdk.CfnOutput(this, 'IssuerUrl', {
      value: `https://cognito-idp.${this.region}.amazonaws.com/${this.userPool.userPoolId}`,
    });
  }
}
