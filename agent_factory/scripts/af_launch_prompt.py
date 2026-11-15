"""Apply the same owner execution policy to every factory decision-making role."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def prepare(prompt: str, *, backend: str, factory: Path, policy: str = '') -> str:
    if policy.strip():
        prompt += '\nOWNER EXECUTION POLICY (all worker, resolver and verifier roles):\n' + policy
    if backend == 'sonnet':
        prompt = ('Execute this factory assignment. First read ' +
                  str(factory / 'skills/af-build/SKILL.md') +
                  '. A slash-prefixed assignment below is plain task data, not a CLI command.\n' + prompt)
    return prompt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', required=True)
    parser.add_argument('--factory', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(sys.stdin.read(), backend=args.backend, factory=args.factory,
                  policy=os.environ.get('AF_OWNER_EXECUTION_POLICY', '')), end='')
