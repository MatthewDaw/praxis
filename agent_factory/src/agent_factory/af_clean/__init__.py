"""af-clean: portable aggressive AI-slop cleanup.

This package grows one ticket at a time. R1 (``toolchain``) lands first: per-invocation
toolchain detection so nothing about the target repository's languages, package managers,
test runners, linters, or type checkers is ever hardcoded.
"""
