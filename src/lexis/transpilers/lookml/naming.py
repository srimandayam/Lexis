"""Slugify Ossie names into LookML identifiers, de-duplicating within a scope.

Ossie names are free-form strings; LookML identifiers must match
`[a-z_][a-z0-9_]*`. Two different Ossie names can therefore collapse onto one
LookML name (`Store Sales` and `store_sales` both slugify to `store_sales`),
which would silently overwrite a view or field. The registry suffixes the second
one and warns instead - never overwrites - and every reference the emitter
writes goes through `lookup()` rather than re-slugifying, so a suffixed name is
referenced consistently everywhere.
"""

import re

_NON_IDENTIFIER_RE = re.compile(r"[^a-z0-9_]+")

#: LookML parameter names that would be ambiguous as a view or field name.
#: Not an exhaustive LookML keyword list - just the ones that read as a
#: parameter in the position a name would appear.
RESERVED = frozenset(
    {"view", "explore", "join", "dimension", "dimension_group", "measure", "filter", "parameter", "set", "include", "connection", "sql", "type"}
)


def slugify(name: str) -> str:
    """Best-effort conversion of an arbitrary Ossie name to a LookML identifier."""
    slug = _NON_IDENTIFIER_RE.sub("_", name.strip().lower()).strip("_")
    if not slug:
        return "unnamed"
    if slug[0].isdigit():
        slug = f"_{slug}"
    return slug


class NameRegistry:
    """Per-scope Ossie-name -> LookML-identifier mapping.

    Scopes keep view names and each view's field names in separate namespaces
    (LookML allows a field and a view to share a name). Use the literal scope
    `"view"` for view names and a view's own slug as the scope for its fields.
    """

    def __init__(self, warnings: list[str]) -> None:
        self._warnings = warnings
        self._mapping: dict[tuple[str, str], str] = {}
        self._taken: dict[str, set[str]] = {}

    def register(self, scope: str, original: str, *, preferred: str | None = None) -> str:
        """Reserve a LookML name for `original` in `scope` and return it.

        `preferred` overrides the slug derived from `original` (used for
        dimension groups, whose LookML name is the field name with its temporal
        suffix stripped). Re-registering the same `original` returns the name
        already assigned.
        """
        key = (scope, original)
        if key in self._mapping:
            return self._mapping[key]

        taken = self._taken.setdefault(scope, set())
        base = preferred if preferred is not None else slugify(original)
        if base in RESERVED:
            base = f"{base}_field"
            self._warnings.append(
                f"{original!r} is a LookML reserved word; emitted as {base!r}."
            )

        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}_{suffix}"
            suffix += 1
        if candidate != base:
            self._warnings.append(
                f"LookML name collision in {scope!r}: {original!r} slugifies to "
                f"{base!r}, already taken - emitted as {candidate!r} instead."
            )

        taken.add(candidate)
        self._mapping[key] = candidate
        return candidate

    def lookup(self, scope: str, original: str) -> str | None:
        """The registered LookML name for `original`, or None if never registered."""
        return self._mapping.get((scope, original))

    def is_taken(self, scope: str, candidate: str) -> bool:
        return candidate in self._taken.get(scope, set())
