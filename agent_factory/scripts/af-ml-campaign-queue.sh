#!/usr/bin/env bash
set -u

cat >&2 <<'EOF'
RETIRED: af-ml-campaign-queue.sh is not a supported campaign controller.
Use a project CampaignLifecycle adapter with:
  python -m knowledge.ml_registry.runtime.campaign_job --config CAMPAIGN_JOB.json
For unattended or multi-campaign operation, use the canonical portfolio operator:
  agent_factory/scripts/af-ml-portfolio-launch.sh --config OPERATOR.json run
EOF
exit 2
