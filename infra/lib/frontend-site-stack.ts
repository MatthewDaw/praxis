import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as cloudfront_origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import { Construct } from 'constructs';

export interface FrontendSiteStackProps extends cdk.StackProps {
  /**
   * Directory holding the built frontend bundle to upload. Defaults to
   * `frontend-react/dist` relative to this file. If it does not exist at synth
   * time (e.g. CI before a build), the `BucketDeployment` is skipped so
   * `cdk synth` never fails — the deploy orchestrator builds it first.
   */
  readonly distPath?: string;
}

/**
 * Static hosting for the PRAXIS frontend SPA.
 *
 * A private S3 bucket (no public access) holds the built Vite bundle and is
 * served through a CloudFront distribution using Origin Access Control. Viewer
 * requests are forced to HTTPS, and 403/404 responses are rewritten to
 * `/index.html` (200) so client-side routing on deep links works.
 *
 * The bucket is DESTROY + autoDeleteObjects since it only holds rebuildable
 * assets, making teardown trivial.
 */
export class FrontendSiteStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: FrontendSiteStackProps = {}) {
    super(scope, id, props);

    this.bucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    this.distribution = new cloudfront.Distribution(this, 'SiteDistribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: cloudfront_origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
        },
      ],
    });

    const distPath = props.distPath ?? path.join(__dirname, '../../frontend-react/dist');
    if (fs.existsSync(distPath)) {
      new s3deploy.BucketDeployment(this, 'DeploySite', {
        sources: [s3deploy.Source.asset(distPath)],
        destinationBucket: this.bucket,
        distribution: this.distribution,
        distributionPaths: ['/*'],
      });
    }

    new cdk.CfnOutput(this, 'DistributionUrl', {
      value: `https://${this.distribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, 'SiteBucketName', { value: this.bucket.bucketName });
  }
}
