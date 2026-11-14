"""Keep inherited application defaults out of an isolated browser fixture."""


def without_inherited_defaults(environment: dict[str, str], defaults: dict[str, str],
                               keys: tuple[str, ...]) -> tuple[dict[str, str], list[str]]:
    result = dict(environment)
    removed = []
    for key in keys:
        if key in defaults and key in result and result[key] == defaults[key]:
            del result[key]
            removed.append(key)
    return result, removed
