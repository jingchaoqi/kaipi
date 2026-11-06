"""Small terminal text layout helpers; widths count cells, not Unicode characters."""

from collections.abc import Sequence

from prompt_toolkit.formatted_text import ANSI, fragment_list_width, to_formatted_text


def columns(rows: Sequence[tuple[str, ...]]) -> list[str]:
    """Align columns with a two-cell gap, preserving ANSI styling in each cell."""
    sizes = [[fragment_list_width(to_formatted_text(ANSI(cell))) for cell in row] for row in rows]
    widths = [max(column) for column in zip(*sizes, strict=True)]
    return [
        "".join(
            cell + (" " * (widths[i] - size[i] + 2) if i < len(row) - 1 else "")
            for i, cell in enumerate(row)
        ).rstrip()
        for row, size in zip(rows, sizes, strict=True)
    ]
