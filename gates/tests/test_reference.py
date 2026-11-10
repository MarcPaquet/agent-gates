"""Specification of docs/reference.md: its states, transitions and refusals tables say what gates/machine.py does."""

import re
import unittest
from pathlib import Path

from gates import machine

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "reference.md"

_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_CODE_SPAN = re.compile(r"(`+)(.*)\1", re.DOTALL)


def table_rows(text: str, heading: str) -> list[list[str]]:
    """Return the data rows of the first Markdown table after the line `heading`, each as a list of cell texts.

    The table is the first run of lines starting with `|` after the heading, before the next heading. Its first
    two lines (header and separator) are dropped. Raises AssertionError when the heading is not in the text.
    """
    lines = text.splitlines()
    if heading not in lines:
        raise AssertionError(f"docs/reference.md has no heading line {heading!r}")
    table: list[str] = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("#"):
            break
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    return [[cell_text(cell) for cell in split_row(line)] for line in table[2:]]


def split_row(line: str) -> list[str]:
    """Split a table row on its unescaped pipes, dropping the outer ones; an escaped pipe `\\|` becomes `|`."""
    inner = line.strip().removeprefix("|").removesuffix("|")
    return [cell.replace("\\|", "|").strip() for cell in _UNESCAPED_PIPE.split(inner)]


def cell_text(cell: str) -> str:
    """Return a cell's text, unwrapped when the whole cell is one code span (`draft` or `` text ``).

    As in Markdown, one space just inside each end of the span is dropped when both ends have one.
    """
    match = _CODE_SPAN.fullmatch(cell)
    if match is None:
        return cell
    inner = match.group(2)
    if len(inner) >= 2 and inner.startswith(" ") and inner.endswith(" ") and inner.strip():
        inner = inner[1:-1]
    return inner


class ReferenceTablesTest(unittest.TestCase):
    """The reference's three tables are the machine's own constants, written out."""

    def setUp(self) -> None:
        self.text = REFERENCE.read_text(encoding="utf-8")

    def test_states_table_lists_exactly_the_machine_states(self) -> None:
        rows = table_rows(self.text, "### States")
        self.assertEqual([row[0] for row in rows], list(machine.STATES))

    def test_transitions_table_has_exactly_the_machine_transitions_and_actors(self) -> None:
        rows = table_rows(self.text, "### Transitions")
        transitions = {
            (row[0], row[1]): tuple(actor.strip() for actor in row[2].split(",")) for row in rows
        }
        self.assertEqual(len(rows), len(transitions), "a transition is listed twice")
        self.assertEqual(transitions, machine.TRANSITIONS)

    def test_refusals_table_has_every_rule_with_its_exact_message(self) -> None:
        rows = table_rows(self.text, "### Refusals")
        refusals = {row[0]: row[1] for row in rows}
        self.assertEqual(len(rows), len(refusals), "a rule is listed twice")
        self.assertEqual(refusals, machine.REFUSALS)


if __name__ == "__main__":
    unittest.main()
