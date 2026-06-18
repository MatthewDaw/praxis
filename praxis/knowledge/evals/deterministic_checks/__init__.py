"""Registered deterministic checks, referenced by cases via dotted ``ref``.

A check is ``Callable[[EvalContext, **params], CheckResult]``. The runner
resolves a :class:`DeterministicCheckRef` to one of these and calls it with the
ref's ``params`` as keyword arguments.
"""
