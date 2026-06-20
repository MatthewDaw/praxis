import * as cdk from 'aws-cdk-lib/core';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { Construct } from 'constructs';

export interface DnsStackProps extends cdk.StackProps {
  /** Apex domain registered at Cloudflare. Defaults to `praxiskg.com`. */
  readonly domainName?: string;
}

/**
 * Route 53 public hosted zone for the PRAXIS domain.
 *
 * The domain is registered at **Cloudflare Registrar**, which forbids pointing
 * the apex nameservers at a third party — so the apex (and `www`) stay on
 * Cloudflare DNS. Instead we delegate individual *subdomains* (`phoenix.`,
 * `app.`, ...) to this zone by adding NS records in the Cloudflare dashboard
 * that list the four nameservers in this stack's `NameServers` output. Route 53
 * is then authoritative for every record under a delegated subdomain, even
 * though the zone carries the apex name.
 *
 * Deploy this FIRST, copy the nameservers into Cloudflare, let the delegation
 * propagate, then deploy the stacks that create records here (e.g. Phoenix).
 */
export class DnsStack extends cdk.Stack {
  public readonly zone: route53.PublicHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps = {}) {
    super(scope, id, props);

    this.zone = new route53.PublicHostedZone(this, 'Zone', {
      zoneName: props.domainName ?? 'praxiskg.com',
    });

    new cdk.CfnOutput(this, 'HostedZoneId', { value: this.zone.hostedZoneId });
    new cdk.CfnOutput(this, 'NameServers', {
      value: cdk.Fn.join(', ', this.zone.hostedZoneNameServers ?? []),
      description:
        'Add these as NS records (one per nameserver, same subdomain name) in Cloudflare for each delegated subdomain',
    });
  }
}
