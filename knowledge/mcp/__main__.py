"""Entry point: uv run python -m knowledge.mcp [login]"""

import sys

from knowledge.mcp.server import main

main(sys.argv[1:])
