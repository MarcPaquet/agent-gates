"""Tests for linkcheck, one per rule of the spec."""

import contextlib
import io
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from example.linkcheck import linkcheck


def run(*args: str) -> tuple[int, str, str]:
    """Run main and return (status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = linkcheck.main(list(args))
    return status, out.getvalue(), err.getvalue()


class LinkcheckTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, name: str, text: str) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_broken_relative_link_is_reported_with_file_and_line(self) -> None:
        f = self.write("a.md", "x\n\n[a](missing.md)\n\n[b](missing.md#intro)\n")
        status, out, _ = run(f)
        self.assertEqual(status, 1)
        self.assertEqual(out, f"{f}:3: missing.md\n{f}:5: missing.md#intro\n")

    def test_fragment_is_ignored_when_resolving(self) -> None:
        self.write("present.md", "hi\n")
        f = self.write("a.md", "[c](present.md#nowhere)\n")
        self.assertEqual(run(f), (0, "", ""))

    def test_output_follows_argument_then_line_order(self) -> None:
        a = self.write("a.md", "[x](nope1.md)\n")
        b = self.write("b.md", "[x](nope2.md)\n[y](nope3.md)\n")
        _, out, _ = run(b, a)
        self.assertEqual(out, f"{b}:1: nope2.md\n{b}:2: nope3.md\n{a}:1: nope1.md\n")

    def test_skipped_targets_are_not_reported_or_fetched(self) -> None:
        f = self.write(
            "a.md",
            "[w](https://example.invalid/x) [h](http://example.invalid/y)\n"
            "[m](mailto:someone@example.invalid)\n[s](#section)\n",
        )
        calls: list[tuple] = []

        def refuse(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise OSError("network is not allowed")

        with mock.patch.object(socket, "socket", refuse), mock.patch.object(
            socket, "getaddrinfo", refuse
        ):
            result = run(f)
        self.assertEqual(result, (0, "", ""))
        self.assertEqual(calls, [])

    def test_directory_files_are_named_from_the_argument_and_sorted(self) -> None:
        self.write("docs/sub/a.md", "[x](gone-a.md)\n")
        self.write("docs/b.md", "ok\n\n[y](gone-b.md#top)\n")
        status, out, _ = run(f"{self.root}/docs/")
        base = f"{self.root}/docs"
        self.assertEqual(status, 1)
        self.assertEqual(
            out, f"{base}/b.md:3: gone-b.md#top\n{base}/sub/a.md:1: gone-a.md\n"
        )

    def test_all_links_resolve_exits_zero_without_output(self) -> None:
        self.write("docs/sub/a.md", "[ok](../b.md)\n")
        self.write("docs/b.md", "no links\n")
        self.assertEqual(run(str(self.root / "docs")), (0, "", ""))

    def test_no_path_is_a_usage_error(self) -> None:
        status, out, err = run()
        self.assertEqual((status, out), (2, ""))
        self.assertTrue(err)

    def test_missing_path_is_a_usage_error(self) -> None:
        missing = str(self.root / "gone")
        status, out, err = run(missing)
        self.assertEqual((status, out), (2, ""))
        self.assertIn(missing, err)

    def test_target_resolves_relative_to_linking_file(self) -> None:
        self.write("README.md", "root\n")
        guide = self.write("docs/guide.md", "[up](../README.md)\n[x](README.md)\n")
        elsewhere = self.write("cwd/README.md", "here\n")
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(Path(elsewhere).parent)
        self.assertEqual(run(guide), (1, f"{guide}:2: README.md\n", ""))
        os.chdir(self.root)
        self.assertEqual(run(guide), (1, f"{guide}:2: README.md\n", ""))


if __name__ == "__main__":
    unittest.main()
