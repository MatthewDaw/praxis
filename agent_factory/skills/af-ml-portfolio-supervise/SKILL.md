---
name: af-ml-portfolio-supervise
description: Run, inspect, stop, resume, or explain a validated ML campaign portfolio through the canonical Praxis controller.
---

# af-ml-portfolio-supervise

This is a thin operator entry point. It does not choose arms, adjudicate runs, move aliases,
define dependencies, or contain campaign IDs. Praxis `/af-ml-supervise` remains the lifecycle
decision authority for one campaign; `finalize` remains the only writer of `production`.

Before `run` or `resume`, validate the project-owned specs and operator JSON. Do not invent a
missing registry, baseline, idea, or artifact. Setup of an explicitly unbootstrapped fixture or
campaign belongs to the campaign job's declared setup adapter; never seed from this skill itself.

Use the canonical surface:

```sh
python -m knowledge.ml_registry.cli.portfolio --config OPERATOR.json status
python -m knowledge.ml_registry.cli.portfolio --config OPERATOR.json explain CAMPAIGN
agent_factory/scripts/af-ml-portfolio-launch.sh --config OPERATOR.json run --poll-interval 10
python -m knowledge.ml_registry.cli.portfolio --config OPERATOR.json stop --drain
python -m knowledge.ml_registry.cli.portfolio --config OPERATOR.json stop --force
python -m knowledge.ml_registry.cli.portfolio --config OPERATOR.json resume
```

`status` reports occupied slots, typed progress and ETA, heartbeat age, named leases, the ready
frontier, dependency waits, retries, and terminal blockers. `stop --drain` stops admission and
waits for current arms. `stop --force` kills every owned process group, supersedes its in-flight
run through the configured registry callback, and releases leases. `resume` reconciles durable
launch intents before admitting work. `explain` reports the exact artifact and resource blocker.

Treat `COMPLETE` written by a campaign job as a claim only. The controller must invoke the real
`RegistryFinalizer.verify`; process exit never moves `production` and never unlocks a consumer.
