import * as cdk from 'aws-cdk-lib/core';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import { Construct } from 'constructs';

export interface KnowledgeGraphDbStackProps extends cdk.StackProps {
  /** Postgres database name created on the instance. Defaults to `praxis_kg`. */
  readonly databaseName?: string;
  /**
   * CIDR allowed to reach the DB port. Defaults to `0.0.0.0/0` so a fresh
   * deploy is reachable, but you should pass your own IP (e.g. `-c
   * allowedCidr=1.2.3.4/32`) to lock it down. The DB still requires the
   * Secrets Manager master credentials regardless.
   */
  readonly allowedCidr?: string;
}

/**
 * The PRAXIS knowledge-graph store: a single PostgreSQL instance with the
 * `vector` (pgvector) extension available, so facts, fact-to-fact edges
 * (contradictions/supports), and embeddings all live in one engine.
 *
 *   facts(id, text, source, confidence, scope, category, observation_count, meta jsonb)
 *   fact_edges(src_id, dst_id, kind)            -- contradiction / supports / ...
 *   facts.embedding vector(N)  + HNSW index     -- cosine search & dedup
 *
 * This is the system of record that replaces the JSON candidate store and the
 * in-process VectorGraph. DynamoDB stays the raw session-log store; this is the
 * distilled knowledge.
 *
 * Networking is deliberately cheap: public subnets only (no NAT gateways),
 * the instance is publiclyAccessible, and a security group gates the port to
 * `allowedCidr`. Master credentials are generated into Secrets Manager.
 */
export class KnowledgeGraphDbStack extends cdk.Stack {
  public readonly instance: rds.DatabaseInstance;

  constructor(scope: Construct, id: string, props: KnowledgeGraphDbStackProps = {}) {
    super(scope, id, props);

    const databaseName = props.databaseName ?? 'praxis_kg';
    const allowedCidr = props.allowedCidr ?? '0.0.0.0/0';

    // Minimal VPC: public subnets only, no NAT gateways (cost), so the DB can
    // be reached directly over its security group.
    const vpc = new ec2.Vpc(this, 'KgVpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    const securityGroup = new ec2.SecurityGroup(this, 'KgDbSg', {
      vpc,
      description: 'PRAXIS knowledge-graph Postgres access',
      allowAllOutbound: true,
    });
    securityGroup.addIngressRule(
      ec2.Peer.ipv4(allowedCidr),
      ec2.Port.tcp(5432),
      `Postgres from ${allowedCidr}`,
    );

    this.instance = new rds.DatabaseInstance(this, 'KgInstance', {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16_4,
      }),
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.BURSTABLE4_GRAVITON,
        ec2.InstanceSize.MICRO,
      ),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [securityGroup],
      publiclyAccessible: true,
      databaseName,
      // Master user + generated password land in Secrets Manager.
      credentials: rds.Credentials.fromGeneratedSecret('praxis', {
        secretName: 'praxis/knowledge-graph/db',
      }),
      allocatedStorage: 20,
      maxAllocatedStorage: 100,
      storageType: rds.StorageType.GP3,
      backupRetention: cdk.Duration.days(7),
      deletionProtection: false,
      removalPolicy: cdk.RemovalPolicy.SNAPSHOT,
    });

    new cdk.CfnOutput(this, 'DbEndpoint', {
      value: this.instance.dbInstanceEndpointAddress,
    });
    new cdk.CfnOutput(this, 'DbPort', {
      value: this.instance.dbInstanceEndpointPort,
    });
    new cdk.CfnOutput(this, 'DbName', { value: databaseName });
    new cdk.CfnOutput(this, 'DbSecretArn', {
      value: this.instance.secret?.secretArn ?? 'none',
    });
    new cdk.CfnOutput(this, 'DbSecretName', {
      value: 'praxis/knowledge-graph/db',
    });
  }
}
