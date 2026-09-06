"""Deterministic proposal linting; textual findings are advisory until approval."""

import re

# Recognize path-only scope fences, including 'Do not modify src/foo.py'.
_PATH = re.compile(r"[/\\]*(?:[\w.@~-]+[/\\])+[\w.*@/\\~-]*|\b[\w-]+\.[a-zA-Z0-9]+\b")
_FENCE = re.compile(
    r"^(?:(?:do not|don't|never)\s+(?:change|modify|edit|touch|delete|remove)\s+"
    r"|(?:only|files?|directories|directory|scope)\s*:?\s*)?"
    r"[`'\"\w./\\*~@, -]+[.!]?$", re.IGNORECASE,
)
_FORBID = re.compile(
    r"(?:do not|don't|never|must not)\s+(?:change|modify|edit|touch|remove|delete|rename)"
    r"\s+(.+?)[.!]?$", re.IGNORECASE,
)
_REQUIRE = re.compile(
    r"\b(?:change|modify|edit|remove|delete|rename|update|add|replace)\s+", re.IGNORECASE,
)


def check_proposal(specs: list[dict], existing_ids: set[str]) -> list[str]:
    """Return stable, human-readable findings without executing commands or editing specs.

    Explicit task ``id`` values are proposal-local dependency references. Text checks
    deliberately recognize simple fences and direct action/target contradictions,
    not arbitrary natural-language implications.
    """
    findings = []
    local = {task['id']: i for i, task in enumerate(specs) if task.get('id')}
    seen = set()
    for task in specs:
        ref = task.get('id')
        if ref:
            if ref in seen or ref in existing_ids:
                findings.append(f"task {ref}: ambiguous dependency id (duplicate or existing task)")
            seen.add(ref)
    graph = [[] for _ in specs]
    for i, task in enumerate(specs):
        label = f"task {task.get('id') or i + 1}"
        for dep in task.get('depends_on', []):
            if dep in local:
                graph[i].append(local[dep])
            elif dep not in existing_ids:
                findings.append(f"{label}: unknown dependency {dep}")
        criteria = task.get('acceptance', [])
        if not any(c.lstrip().startswith('$') for c in criteria):
            findings.append(f"{label}: no machine-checkable acceptance criterion starting with '$'")
        for boundary in task.get('boundaries', []):
            # Remove the path itself: prose remaining beyond a scope-fence prefix
            # describes an invariant, e.g. 'src/api.py must remain compatible'.
            remainder = _PATH.sub('', boundary).strip(' `\"\'.,:;')
            if _PATH.search(boundary) and _FENCE.fullmatch(boundary) and (
                not remainder or re.fullmatch(
                    r"(?:(?:do not|don't|never) (?:change|modify|edit|touch|delete|remove)"
                    r"|only|files?|directories|directory|scope)", remainder, re.IGNORECASE
                )
            ):
                findings.append(f"{label}: boundary names a path instead of an invariant: {boundary}")
            forbidden = _FORBID.search(boundary)
            if forbidden:
                target = forbidden[1].strip(' `\"\'').lower()
                for criterion in criteria:
                    if criterion.lstrip().startswith('$'):
                        continue  # A test command mentioning a file does not require editing it.
                    for action in _REQUIRE.finditer(criterion):
                        prefix = criterion[:action.start()].lower().rstrip()
                        if prefix.endswith(('not', "don't", 'never')):
                            continue
                        required = criterion[action.end():].strip(' `\"\'').lower()
                        if target and re.match(re.escape(target) + r"(?:\b|$)", required):
                            findings.append(
                                f"{label}: acceptance conflicts with boundary: "
                                f"{criterion!r} / {boundary!r}"
                            )
                            break

    # Iterative DFS avoids recursion limits for large proposals.
    colors = [0] * len(specs)
    for start in range(len(specs)):
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                colors[node] = 2
            elif colors[node] == 0:
                colors[node] = 1
                stack.append((node, True))
                for dep in reversed(graph[node]):
                    if colors[dep] == 1:
                        findings.append(
                            f"dependency cycle inside proposal: "
                            f"{specs[node].get('id') or node + 1} -> "
                            f"{specs[dep].get('id') or dep + 1}"
                        )
                    elif colors[dep] == 0:
                        stack.append((dep, False))
    return findings
