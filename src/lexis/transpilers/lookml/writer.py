"""Minimal LookML serializer.

LookML is a small, regular block language - `key: value` parameters and nested
`kind: name { ... }` blocks - so this hand-rolls the ~100 lines of serialization
rather than taking on a runtime dependency, the same way
`transpilers/snowflake_semantic_view.py` hand-rolls Snowflake DDL. The `lkml`
package (dev-only) is used in the tests as an *independent* parser to prove what
we write here is well formed, which is worth more than using its writer.

Three value flavours, because LookML spells them differently and getting one
wrong is a parse error rather than a visible mistake:

- `Bare("sum")`        -> `type: sum`            (unquoted token or `[a, b]` list)
- `"some text"`        -> `label: "some text"`   (quoted, escaped)
- `Sql("${TABLE}.x")`  -> `sql: ${TABLE}.x ;;`   (raw, `;;`-terminated)
"""

from dataclasses import dataclass, field
from typing import Union

INDENT = "  "


class Bare(str):
    """A value emitted verbatim: `yes`, `sum`, `many_to_one`, `[raw, date]`."""


class Sql(str):
    """A value emitted raw and terminated with `;;` (LookML's SQL blocks)."""


Value = Union[Bare, Sql, str]


def bare_list(items: list[str]) -> Bare:
    """`[raw, date, week]` - an unquoted LookML list, e.g. `timeframes`."""
    return Bare("[" + ", ".join(items) + "]")


def quoted_list(items: list[str]) -> Bare:
    """`["a", "b"]` - a list of quoted strings, e.g. `tags`."""
    return Bare("[" + ", ".join(quote(i) for i in items) + "]")


def quote(text: str) -> str:
    """Render `text` as a LookML double-quoted string.

    LookML strings are single-line with backslash escapes, so embedded newlines
    (common in Ossie `description`/`ai_context` prose) are collapsed to spaces
    rather than emitted literally, which would truncate the value at the newline.
    """
    collapsed = " ".join(text.split())
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class Block:
    """A LookML block: `kind: name { params... children... }`.

    A `kind` with no `name` (only used for the model file's own top-level
    parameter list) renders its parameters bare, with no wrapping braces.
    """

    kind: str
    name: str | None = None
    params: list[tuple[str, Value]] = field(default_factory=list)
    children: list["Block"] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)

    def add(self, key: str, value: Value | None) -> "Block":
        """Append `key: value`, skipping None so callers can pass optionals directly."""
        if value is not None:
            self.params.append((key, value))
        return self


def render_value(key: str, value: Value) -> str:
    if isinstance(value, Sql):
        if ";;" in value:
            raise ValueError(
                f"LookML {key!r} value contains ';;', which would terminate the "
                f"block early: {value!r}"
            )
        return f"{key}: {value} ;;"
    if isinstance(value, Bare):
        return f"{key}: {value}"
    return f"{key}: {quote(value)}"


def render(block: Block, level: int = 0) -> str:
    pad = INDENT * level
    lines = [f"{pad}# {c}" for c in block.comments]

    if block.name is None and not block.children and block.kind == "":
        # Bare parameter list (the model file's `connection:` / `include:` header).
        return "\n".join([*lines, *(f"{pad}{render_value(k, v)}" for k, v in block.params)])

    header = f"{pad}{block.kind}: {block.name} {{" if block.name else f"{pad}{block.kind} {{"
    lines.append(header)
    for key, value in block.params:
        lines.append(f"{pad}{INDENT}{render_value(key, value)}")
    for child in block.children:
        lines.append("")
        lines.append(render(child, level + 1))
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def render_file(header_comments: list[str], blocks: list[Block]) -> str:
    """Render a whole `.lkml` file: leading `#` comments, then blank-line-separated blocks."""
    parts = ["\n".join(f"# {c}" for c in header_comments)] if header_comments else []
    parts.extend(render(b) for b in blocks)
    return "\n\n".join(p for p in parts if p) + "\n"
