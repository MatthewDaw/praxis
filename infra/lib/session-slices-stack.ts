import * as cdk from 'aws-cdk-lib/core';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';
import { SESSION_SLICES } from './config';

export interface SessionSlicesStackProps extends cdk.StackProps {
  /** S3 bucket holding raw transcript slices. Defaults to `praxis-session-slices`. */
  readonly bucketName?: string;
  /** DynamoDB table holding derived insights. Defaults to `praxis-session-insights`. */
  readonly insightsTableName?: string;
  /** Days a raw slice lives before lifecycle expiry. Defaults to 14. */
  readonly sliceRetentionDays?: number;
}

/**
 * The PRAXIS session-capture store, redesigned for a write-once / read-once
 * pipeline: the `claude-trace` launcher uploads a session's transcript to S3
 * when a PR ships (git push / gh pr create); a remote extractor reads each slice
 * exactly once, writes the derived insights, and the raw slice ages out.
 *
 * Two resources:
 *
 *   1. slices bucket — raw transcripts, one object per (org, user, repo, branch,
 *      session). EventBridge notifications are on so the extractor can subscribe
 *      to ObjectCreated without this stack owning the compute. A lifecycle rule
 *      auto-expires objects after `sliceRetentionDays`, because nothing reads a
 *      slice twice. Private, TLS-enforced, SSE-S3.
 *
 *   2. insights table — the small, durable output. Point lookups, never scans:
 *        PK = PR#<owner/repo>#<branch>   SK = SESSION#<sessionId>   — insight record
 *      GSI1 lets you browse by tenant without a scan:
 *        GSI1PK = ORG#<org>#USER#<user>  GSI1SK = TS#<iso8601>
 *
 * No raw "session logs" table: the firehose lives in S3 and expires, so the
 * dataset that has to be queried stays small.
 */
export class SessionSlicesStack extends cdk.Stack {
  public readonly slicesBucket: s3.Bucket;
  public readonly insightsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: SessionSlicesStackProps = {}) {
    super(scope, id, props);

    this.slicesBucket = new s3.Bucket(this, 'SlicesBucket', {
      bucketName: props.bucketName ?? SESSION_SLICES.bucketName,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      eventBridgeEnabled: true,
      lifecycleRules: [
        {
          id: 'expire-raw-slices',
          enabled: true,
          expiration: cdk.Duration.days(props.sliceRetentionDays ?? 14),
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.insightsTable = new dynamodb.Table(this, 'InsightsTable', {
      tableName: props.insightsTableName ?? SESSION_SLICES.insightsTableName,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.insightsTable.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new cdk.CfnOutput(this, 'SlicesBucketName', { value: this.slicesBucket.bucketName });
    new cdk.CfnOutput(this, 'SlicesBucketArn', { value: this.slicesBucket.bucketArn });
    new cdk.CfnOutput(this, 'InsightsTableName', { value: this.insightsTable.tableName });
    new cdk.CfnOutput(this, 'InsightsTableArn', { value: this.insightsTable.tableArn });
  }
}
