import * as cdk from 'aws-cdk-lib/core';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

/**
 * Shared network for the PRAXIS stack: one cheap VPC for every VPC-bound
 * workload — the Phoenix EC2 instance and the knowledge-graph RDS instance.
 * New VPC-bound stacks should consume this rather than minting their own.
 *
 * Previously Phoenix and the KG DB each minted their own identical VPC. Moving
 * the KG DB here required replacing the (already-deployed) RDS instance, since
 * an instance can't change VPC in place; its data was dumped and restored
 * around the swap.
 *
 * Public subnets only, no NAT gateways: every workload here is reached directly
 * over its own security group (RDS publiclyAccessible, Phoenix behind an
 * Elastic IP), so NAT would add cost for no benefit.
 */
export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props: cdk.StackProps = {}) {
    super(scope, id, props);

    this.vpc = new ec2.Vpc(this, 'PraxisVpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    new cdk.CfnOutput(this, 'VpcId', { value: this.vpc.vpcId });
  }
}
