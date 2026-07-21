import * as cdk from 'aws-cdk-lib/core';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { DEFAULT_ALLOWED_CIDR } from './config';

export interface DevBoxStackProps extends cdk.StackProps {
  /** Shared VPC the instance and its workspace volume live in (see NetworkStack). */
  readonly vpc: ec2.IVpc;
  /**
   * EC2 instance type string (e.g. `t3.xlarge`, `m7i.xlarge`, or a GPU type
   * like `g5.xlarge`). Defaults to a CPU box; switch to a `g`/`p`-family type
   * (with a driver-equipped AMI via `amiId`) to give the box a real GPU. That
   * change replaces the instance — which is why the workspace lives on its own
   * retained volume (see below), so your code survives the swap.
   */
  readonly instanceType?: string;
  /**
   * CIDR allowed to reach SSH (port 22) — the PyCharm remote-dev channel.
   * Defaults to `0.0.0.0/0` so a fresh deploy is reachable; **lock this to your
   * own IP** (`-c devBoxAllowedSshCidr=1.2.3.4/32`) since key-only SSH is the
   * only thing standing between the world and this box.
   */
  readonly allowedSshCidr?: string;
  /**
   * OpenSSH public key material to authorize (e.g. the contents of your
   * `~/.ssh/id_ed25519.pub`). When set, the box trusts a key you already hold —
   * the cleanest path for PyCharm. When omitted, the stack mints a fresh key
   * pair and stashes the private key in SSM Parameter Store for you to fetch.
   */
  readonly sshPublicKey?: string;
  /** Root EBS volume size (GiB). Defaults to 30. */
  readonly rootVolumeGib?: number;
  /** Retained `/workspace` EBS volume size (GiB). Defaults to 100. */
  readonly workspaceVolumeGib?: number;
  /**
   * Override AMI id for this stack's region. Use to boot a GPU/Deep-Learning
   * AMI (NVIDIA drivers preinstalled) when you flip `instanceType` to a
   * GPU family. Omit for the default Amazon Linux 2023 x86_64 image.
   */
  readonly amiId?: string;
}

/**
 * A remote dev box for driving long-running Claude Code sessions from PyCharm.
 *
 * Unlike the SSM-only Phoenix box, this instance exposes **SSH** — PyCharm's
 * remote development / Gateway backend connects over SSH, so port 22 (locked to
 * `allowedSshCidr`) and a key pair are the point of the stack. SSM Session
 * Manager is wired up too as a break-glass path if you ever lock yourself out.
 *
 * It boots as a plain CPU box ("gpu-enabled but without a gpu"): the AMI and
 * instance type are context-overridable, so switching to a `g5.xlarge` on a
 * Deep-Learning AMI later is a one-line `cdk deploy` — no code change. Because
 * that swap *replaces* the instance, all your work lives on a separate
 * **RETAIN**-policy EBS volume mounted at `/workspace`: the box is cattle, the
 * volume is the pet (same design as PhoenixStack). Snapshot it for backup.
 *
 * First boot installs the tooling a Claude session on this repo needs: git,
 * tmux (so sessions survive SSH disconnects), Node 20 + the Claude Code CLI,
 * `uv` (this repo's Python toolchain), Docker, and ripgrep. Auth (`claude
 * login`) is interactive and left to you.
 *
 * Networking follows the sibling stacks: public subnet, no NAT, reached over an
 * Elastic IP so the SSH host stays stable across reboots (PyCharm saves it).
 */
export class DevBoxStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DevBoxStackProps) {
    super(scope, id, props);

    const allowedSshCidr = props.allowedSshCidr ?? DEFAULT_ALLOWED_CIDR;
    const rootVolumeGib = props.rootVolumeGib ?? 30;
    const workspaceVolumeGib = props.workspaceVolumeGib ?? 100;
    const instanceType = new ec2.InstanceType(props.instanceType ?? 't3.xlarge');

    const vpc = props.vpc;
    // Pin the instance and its workspace volume to one AZ — an EBS volume can
    // only attach to an instance in its own AZ.
    const subnet = vpc.publicSubnets[0];

    const securityGroup = new ec2.SecurityGroup(this, 'DevBoxSg', {
      vpc,
      description: 'PRAXIS dev box - SSH for PyCharm remote development',
      allowAllOutbound: true,
    });
    securityGroup.addIngressRule(
      ec2.Peer.ipv4(allowedSshCidr),
      ec2.Port.tcp(22),
      `SSH (PyCharm remote dev) from ${allowedSshCidr}`,
    );

    // SSH key pair. Bring your own public key (best for PyCharm) or let the
    // stack mint one and drop the private key into SSM Parameter Store.
    let keyPair: ec2.IKeyPair;
    let privateKeyParamName: string | undefined;
    if (props.sshPublicKey) {
      keyPair = new ec2.KeyPair(this, 'DevBoxKey', {
        keyPairName: 'praxis-devbox',
        publicKeyMaterial: props.sshPublicKey,
      });
    } else {
      const generated = new ec2.KeyPair(this, 'DevBoxKey', {
        keyPairName: 'praxis-devbox',
        type: ec2.KeyPairType.ED25519,
      });
      keyPair = generated;
      privateKeyParamName = generated.privateKey.parameterName;
    }

    // SSM Session Manager as a break-glass path (no port, no key).
    const role = new iam.Role(this, 'DevBoxRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
        // The dev box runs `cdk deploy` (and long agent-factory `/af-build` runs
        // that deploy app stacks) directly from the instance, so the instance role
        // needs full deploy rights. AdministratorAccess keeps the box a
        // self-sufficient deploy host and prevents this grant from drifting away on
        // the next `cdk deploy PraxisDevBoxStack`.
        iam.ManagedPolicy.fromAwsManagedPolicyName('AdministratorAccess'),
      ],
    });

    // The retained workspace volume. RETAIN so an instance-type/AMI swap (or a
    // stack teardown) leaves your code intact; the boot script formats it only
    // on first use.
    const workspace = new ec2.Volume(this, 'DevBoxWorkspace', {
      availabilityZone: subnet.availabilityZone,
      size: cdk.Size.gibibytes(workspaceVolumeGib),
      volumeType: ec2.EbsDeviceVolumeType.GP3,
      encrypted: true,
    });
    workspace.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const machineImage = props.amiId
      ? ec2.MachineImage.genericLinux({ [this.region]: props.amiId })
      : ec2.MachineImage.latestAmazonLinux2023({
          cpuType: ec2.AmazonLinuxCpuType.X86_64,
        });

    const instance = new ec2.Instance(this, 'DevBoxInstance', {
      vpc,
      vpcSubnets: { subnets: [subnet] },
      instanceType,
      machineImage,
      securityGroup,
      role,
      keyPair,
      userData: ec2.UserData.custom(this.bootScript()),
      requireImdsv2: true,
      blockDevices: [
        {
          deviceName: '/dev/xvda', // AL2023 root
          volume: ec2.BlockDeviceVolume.ebs(rootVolumeGib, {
            volumeType: ec2.EbsDeviceVolumeType.GP3,
            encrypted: true,
          }),
        },
      ],
    });

    // Attach the workspace volume. The boot script discovers it as "the NVMe
    // disk with no mountpoint", so the device alias here is not load-bearing.
    new ec2.CfnVolumeAttachment(this, 'DevBoxWorkspaceAttachment', {
      device: '/dev/sdf',
      instanceId: instance.instanceId,
      volumeId: workspace.volumeId,
    });

    // Stable address so the SSH host (saved in PyCharm) survives reboots.
    const eip = new ec2.CfnEIP(this, 'DevBoxEip', { domain: 'vpc' });
    new ec2.CfnEIPAssociation(this, 'DevBoxEipAssociation', {
      allocationId: eip.attrAllocationId,
      instanceId: instance.instanceId,
    });

    new cdk.CfnOutput(this, 'ElasticIp', { value: eip.ref });
    new cdk.CfnOutput(this, 'InstanceId', { value: instance.instanceId });
    new cdk.CfnOutput(this, 'SshCommand', {
      value: `ssh ec2-user@${eip.ref}`,
    });
    if (privateKeyParamName) {
      new cdk.CfnOutput(this, 'PrivateKeyParam', { value: privateKeyParamName });
      new cdk.CfnOutput(this, 'FetchPrivateKey', {
        value:
          `aws ssm get-parameter --name ${privateKeyParamName} --with-decryption ` +
          `--query Parameter.Value --output text > ~/.ssh/praxis-devbox.pem && ` +
          `chmod 600 ~/.ssh/praxis-devbox.pem`,
      });
    }
    new cdk.CfnOutput(this, 'PyCharmHint', {
      value:
        'PyCharm > Remote Development > SSH: host <ElasticIp>, user ec2-user, ' +
        'your key. Run `claude` inside `tmux` so sessions outlive disconnects.',
    });
  }

  /**
   * First-boot script: mount the retained workspace volume, then install the
   * toolchain a Claude Code session on this repo needs. Idempotent enough to
   * survive re-runs; `claude login` stays interactive (left to the operator).
   */
  private bootScript(): string {
    return `#!/bin/bash
set -euxo pipefail

# --- Mount the retained workspace volume (survives instance replacement) ---
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

mkdir -p /workspace
if ! blkid "$DATA_DEV"; then mkfs -t ext4 -L workspace "$DATA_DEV"; fi
UUID=$(blkid -s UUID -o value "$DATA_DEV")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /workspace ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a
chown ec2-user:ec2-user /workspace

# --- Base dev tooling ---
dnf install -y git tmux ripgrep gcc gcc-c++ make jq docker
systemctl enable --now docker
usermod -aG docker ec2-user

# --- Node 20 (Claude Code CLI needs Node >= 18) + the CLI itself ---
curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -
dnf install -y nodejs
npm install -g @anthropic-ai/claude-code

# --- uv: this repo's Python toolchain ---
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# --- Drop new shells into /workspace ---
grep -q 'cd /workspace' /home/ec2-user/.bashrc || \\
  echo 'cd /workspace 2>/dev/null || true' >> /home/ec2-user/.bashrc
`;
  }
}
