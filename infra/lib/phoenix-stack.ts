import * as cdk from 'aws-cdk-lib/core';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { Construct } from 'constructs';
import { DEFAULT_ALLOWED_CIDR, GRAVITON } from './config';

export interface PhoenixStackProps extends cdk.StackProps {
  /** Shared VPC the instance and its data volume live in (see NetworkStack). */
  readonly vpc: ec2.IVpc;
  /**
   * Arize Phoenix container image tag. The image is `arizephoenix/phoenix`;
   * pin a concrete release for production. Defaults to the latest verified
   * release at authoring time — bump it (and re-`cdk deploy`) to upgrade.
   */
  readonly imageTag?: string;
  /**
   * Public DNS name for the UI (e.g. `phoenix.example.com`). When set, Caddy
   * obtains a real Let's Encrypt certificate — which requires an A record
   * pointing at this stack's Elastic IP *and* ports 80/443 reachable from the
   * public internet (see `allowedWebCidr`). When unset, Caddy serves HTTPS with
   * a self-signed cert (`tls internal`) on the Elastic IP — encrypted, but
   * browsers and OTLP exporters must trust/skip-verify the cert.
   */
  readonly domain?: string;
  /**
   * CIDR allowed to reach the web UI / OTLP ingestion (ports 80 + 443).
   * Defaults to `0.0.0.0/0` so a fresh deploy is reachable; Phoenix's own auth
   * gates access. NOTE: locking this below `0.0.0.0/0` breaks Let's Encrypt
   * (its validation servers can't reach you) — use the self-signed fallback
   * (omit `domain`) if you restrict it.
   */
  readonly allowedWebCidr?: string;
  /** Size (GiB) of the retained EBS data volume. Defaults to 20. */
  readonly dataVolumeGib?: number;
  /**
   * Route 53 hosted zone to create the public A record in. When supplied
   * together with `domain`, this stack points `domain` at the Elastic IP, so
   * Caddy's Let's Encrypt challenge resolves. Omit to manage DNS elsewhere
   * (e.g. straight in Cloudflare).
   */
  readonly hostedZone?: route53.IHostedZone;
}

/**
 * Self-hosted Arize Phoenix LLM-tracing UI for the PRAXIS eval suite.
 *
 * A single `t4g.small` runs two containers (Phoenix + Caddy for TLS) via Docker.
 * Phoenix persists to SQLite — adequate for a handful of users at this trace
 * volume — and PRAXIS's eval harness ships OTLP spans to `https://<host>/v1/traces`.
 *
 * The design choice that makes this safe to operate: **all durable state lives
 * on a separate, encrypted EBS volume with a RETAIN removal policy** — the
 * SQLite DB, the generated secrets, and Caddy's issued certificates. If the
 * instance is replaced (AMI bump, instance-type change, `cdk deploy` that
 * recreates it), the volume detaches and re-attaches to the new box, and the
 * boot script finds the existing filesystem instead of reformatting. The
 * instance is cattle; the volume is the pet. Snapshot the volume for backup.
 *
 * Networking follows the sibling stacks: public subnets only, no NAT gateways.
 * Admin access is via SSM Session Manager (no SSH port, no key pair).
 */
export class PhoenixStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PhoenixStackProps) {
    super(scope, id, props);

    const imageTag = props.imageTag ?? 'version-17.9.0';
    const domain = props.domain ?? '';
    const allowedWebCidr = props.allowedWebCidr ?? DEFAULT_ALLOWED_CIDR;
    const dataVolumeGib = props.dataVolumeGib ?? 20;

    const vpc = props.vpc;

    // Pin the instance and its data volume to one AZ — an EBS volume can only
    // attach to an instance in its own AZ.
    const subnet = vpc.publicSubnets[0];

    const securityGroup = new ec2.SecurityGroup(this, 'PhoenixSg', {
      vpc,
      description: 'PRAXIS Phoenix web UI / OTLP ingestion',
      allowAllOutbound: true,
    });
    securityGroup.addIngressRule(
      ec2.Peer.ipv4(allowedWebCidr),
      ec2.Port.tcp(443),
      `HTTPS from ${allowedWebCidr}`,
    );
    // Port 80 is needed for Caddy's Let's Encrypt HTTP-01 challenge + the
    // HTTP->HTTPS redirect. Harmless when running self-signed.
    securityGroup.addIngressRule(
      ec2.Peer.ipv4(allowedWebCidr),
      ec2.Port.tcp(80),
      `HTTP (ACME / redirect) from ${allowedWebCidr}`,
    );

    // SSM Session Manager for shell access — no SSH, no key pair, no open port 22.
    const role = new iam.Role(this, 'PhoenixRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
      ],
    });

    // The retained data volume. RETAIN so a stack/instance teardown leaves the
    // traces intact; the boot script formats it only on first use.
    const dataVolume = new ec2.Volume(this, 'PhoenixData', {
      availabilityZone: subnet.availabilityZone,
      size: cdk.Size.gibibytes(dataVolumeGib),
      volumeType: ec2.EbsDeviceVolumeType.GP3,
      encrypted: true,
    });
    dataVolume.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const userData = ec2.UserData.custom(this.bootScript(imageTag, domain));

    const instance = new ec2.Instance(this, 'PhoenixInstance', {
      vpc,
      vpcSubnets: { subnets: [subnet] },
      instanceType: ec2.InstanceType.of(GRAVITON, ec2.InstanceSize.SMALL),
      machineImage: ec2.MachineImage.latestAmazonLinux2023({
        cpuType: ec2.AmazonLinuxCpuType.ARM_64,
      }),
      securityGroup,
      role,
      userData,
      requireImdsv2: true,
      blockDevices: [
        {
          deviceName: '/dev/xvda', // AL2023 root
          volume: ec2.BlockDeviceVolume.ebs(16, {
            volumeType: ec2.EbsDeviceVolumeType.GP3,
            encrypted: true,
          }),
        },
      ],
    });

    // Attach the data volume. The boot script discovers it by "the NVMe disk
    // with no mountpoint", so the device alias here is not load-bearing.
    new ec2.CfnVolumeAttachment(this, 'PhoenixDataAttachment', {
      device: '/dev/sdf',
      instanceId: instance.instanceId,
      volumeId: dataVolume.volumeId,
    });

    // Stable address so DNS / OTLP exporters don't break across reboots.
    const eip = new ec2.CfnEIP(this, 'PhoenixEip', { domain: 'vpc' });
    new ec2.CfnEIPAssociation(this, 'PhoenixEipAssociation', {
      allocationId: eip.attrAllocationId,
      instanceId: instance.instanceId,
    });

    // Point the domain at the Elastic IP so Caddy's Let's Encrypt HTTP-01
    // challenge resolves. Only when a zone is supplied AND a domain is set —
    // the self-signed fallback (no domain) needs no DNS.
    if (props.hostedZone && domain) {
      new route53.ARecord(this, 'PhoenixARecord', {
        zone: props.hostedZone,
        recordName: domain,
        target: route53.RecordTarget.fromIpAddresses(eip.ref),
        ttl: cdk.Duration.minutes(5),
      });
    }

    const baseUrl = domain ? `https://${domain}` : `https://${eip.ref}`;
    new cdk.CfnOutput(this, 'ElasticIp', { value: eip.ref });
    new cdk.CfnOutput(this, 'Url', { value: baseUrl });
    new cdk.CfnOutput(this, 'OtlpTracesEndpoint', { value: `${baseUrl}/v1/traces` });
    new cdk.CfnOutput(this, 'InstanceId', { value: instance.instanceId });
    new cdk.CfnOutput(this, 'AdminPasswordHint', {
      value:
        'aws ssm start-session --target <InstanceId>, then: sudo cat /opt/phoenix-data/secrets.env',
    });
  }

  /**
   * First-boot script: mount the retained volume, generate-once secrets onto
   * it, and run Phoenix + Caddy. Secrets live on the data volume (not in
   * CloudFormation) because the encryption key and admin credential are coupled
   * to the data's lifecycle — they must stay stable across instance replacement
   * for the existing SQLite DB to remain usable.
   */
  private bootScript(imageTag: string, domain: string): string {
    // With a domain, Caddy gets a real Let's Encrypt cert. Without one, a bare
    // `:443 { tls internal }` has no hostname to issue a cert for and the TLS
    // handshake fails — so the no-domain path uses on-demand internal issuance,
    // which mints a self-signed cert per SNI. (Caddy warns this is unprotected;
    // it's fine for internal issuance, and setting `domain` removes it.)
    const caddyfile = domain
      ? `${domain} {\n    reverse_proxy phoenix:6006\n}`
      : `:443 {\n    tls internal {\n        on_demand\n    }\n    reverse_proxy phoenix:6006\n}`;

    return `#!/bin/bash
set -euxo pipefail

# --- Find + mount the retained data volume (wait for attachment to settle) ---
DATA_DEV=""
for i in $(seq 1 30); do
  for dev in $(lsblk -dpno NAME | grep -E '/nvme[0-9]+n1$'); do
    if lsblk -no MOUNTPOINT "$dev" | grep -q '/'; then continue; fi
    DATA_DEV="$dev"; break
  done
  [ -n "$DATA_DEV" ] && break
  sleep 2
done
test -n "$DATA_DEV"

mkdir -p /opt/phoenix-data
if ! blkid "$DATA_DEV"; then mkfs -t ext4 -L phoenix "$DATA_DEV"; fi
UUID=$(blkid -s UUID -o value "$DATA_DEV")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /opt/phoenix-data ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a
mkdir -p /opt/phoenix-data/caddy

# --- Generate-once secrets, persisted on the data volume ---
SECRETS=/opt/phoenix-data/secrets.env
if [ ! -f "$SECRETS" ]; then
  echo "PHOENIX_SECRET=$(openssl rand -hex 32)" >  "$SECRETS"
  echo "PHOENIX_ADMIN_PW=$(openssl rand -hex 16)" >> "$SECRETS"
  chmod 600 "$SECRETS"
fi
# shellcheck disable=SC1090
. "$SECRETS"

# --- Docker ---
dnf install -y docker
systemctl enable --now docker
docker network inspect phoenixnet >/dev/null 2>&1 || docker network create phoenixnet

# --- Phoenix (SQLite on the retained volume, auth on) ---
docker rm -f phoenix 2>/dev/null || true
docker run -d --name phoenix --restart=always --network phoenixnet \\
  -v /opt/phoenix-data:/data \\
  -e PHOENIX_WORKING_DIR=/data \\
  -e PHOENIX_SQL_DATABASE_URL=sqlite:////data/phoenix.db \\
  -e PHOENIX_ENABLE_AUTH=true \\
  -e PHOENIX_SECRET="$PHOENIX_SECRET" \\
  -e PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD="$PHOENIX_ADMIN_PW" \\
  arizephoenix/phoenix:${imageTag}

# --- Caddy (TLS terminator / reverse proxy -> phoenix:6006) ---
cat > /opt/phoenix-data/Caddyfile <<'CADDY'
${caddyfile}
CADDY

docker rm -f caddy 2>/dev/null || true
docker run -d --name caddy --restart=always --network phoenixnet \\
  -p 80:80 -p 443:443 \\
  -v /opt/phoenix-data/caddy:/data \\
  -v /opt/phoenix-data/Caddyfile:/etc/caddy/Caddyfile:ro \\
  caddy:2
`;
  }
}
