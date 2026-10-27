from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from scripts.binary_size import (
    BinarySizeError,
    append_delta_table,
    build_report,
    markdown_report,
    parse_macho_sections,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class BinarySizeCliTests(unittest.TestCase):
    def test_empty_delta_table_uses_generic_message(self) -> None:
        lines: list[str] = []

        append_delta_table(lines, "## Segment changes", {})

        self.assertEqual(["", "## Segment changes", "", "No changes detected."], lines)

    def test_threshold_increase_emits_warning_and_exact_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="y2-binary-size-") as tmp:
            root = pathlib.Path(tmp)
            base_binary = root / "base-y2"
            head_binary = root / "head-y2"
            base_binary.write_bytes(b"b" * 100_000)
            head_binary.write_bytes(b"h" * 152_429)
            base_sections = root / "base-sections.txt"
            head_sections = root / "head-sections.txt"
            base_sections.write_text(
                "Segment __TEXT: 65536\n"
                "\tSection __text: 50000\n"
                "\ttotal 50000\n"
                "Segment __LINKEDIT: 32768\n"
                "total 98304\n",
                encoding="utf-8",
            )
            head_sections.write_text(
                "Segment __TEXT: 114688\n"
                "\tSection __text: 102429\n"
                "\ttotal 102429\n"
                "Segment __LINKEDIT: 32768\n"
                "total 147456\n",
                encoding="utf-8",
            )
            json_path = root / "report.json"
            markdown_path = root / "report.md"
            github_output = root / "github-output.txt"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.binary_size",
                    "--base-binary",
                    str(base_binary),
                    "--head-binary",
                    str(head_binary),
                    "--base-sections",
                    str(base_sections),
                    "--head-sections",
                    str(head_sections),
                    "--base-sha",
                    "a" * 40,
                    "--head-sha",
                    "b" * 40,
                    "--target",
                    "aarch64-macos",
                    "--warning-bytes",
                    "52429",
                    "--output-json",
                    str(json_path),
                    "--output-markdown",
                    str(markdown_path),
                    "--github-output",
                    str(github_output),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(1, report["schema_version"])
            self.assertEqual("warning", report["status"])
            self.assertEqual(52_429, report["delta"]["size_bytes"])
            self.assertEqual(52_429, report["section_deltas_bytes"]["__TEXT.__text"])
            self.assertEqual(49_152, report["segment_deltas_bytes"]["__TEXT"])
            self.assertEqual(100_000, report["base"]["size_bytes"])
            self.assertEqual(152_429, report["head"]["size_bytes"])
            self.assertEqual("a" * 40, report["base"]["source_sha"])
            self.assertEqual("b" * 40, report["head"]["source_sha"])
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("+52,429 bytes", markdown)
            self.assertIn("52,429 bytes (0.050000 MiB)", markdown)
            self.assertIn("## Segment changes", markdown)
            self.assertIn("| `__TEXT` | +49,152 |", markdown)
            self.assertIn("## Largest section changes", markdown)
            self.assertIn("| `__TEXT.__text` | +52,429 |", markdown)
            self.assertEqual(
                "warning=true\ndelta_bytes=52429\nstatus=warning\n",
                github_output.read_text(encoding="utf-8"),
            )

    def test_linux_report_attributes_elf_section_growth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="y2-binary-size-") as tmp:
            root = pathlib.Path(tmp)
            base_binary = root / "base-y2"
            head_binary = root / "head-y2"
            base_binary.write_bytes(b"b" * 100_000)
            head_binary.write_bytes(b"h" * 100_100)
            base_sections = root / "base-sections.txt"
            head_sections = root / "head-sections.txt"
            base_sections.write_text(
                f"{base_binary}  :\n"
                "section              size       addr\n"
                ".text               70000      16384\n"
                ".rodata             20000      86016\n"
                ".data                1000     106496\n"
                "Total               91000\n",
                encoding="utf-8",
            )
            head_sections.write_text(
                f"{head_binary}  :\n"
                "section              size       addr\n"
                ".text               70060      16384\n"
                ".rodata             20040      86016\n"
                ".data                1000     106496\n"
                "Total               91100\n",
                encoding="utf-8",
            )
            json_path = root / "report.json"
            markdown_path = root / "report.md"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.binary_size",
                    "--base-binary",
                    str(base_binary),
                    "--head-binary",
                    str(head_binary),
                    "--base-sections",
                    str(base_sections),
                    "--head-sections",
                    str(head_sections),
                    "--base-sha",
                    "a" * 40,
                    "--head-sha",
                    "b" * 40,
                    "--target",
                    "x86_64-linux",
                    "--output-json",
                    str(json_path),
                    "--output-markdown",
                    str(markdown_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual({}, report["segment_deltas_bytes"])
            self.assertEqual(60, report["section_deltas_bytes"][".text"])
            self.assertEqual(40, report["section_deltas_bytes"][".rodata"])
            self.assertIn("| `.text` | +60 |", markdown_path.read_text())

    def test_section_parser_rejects_duplicate_architecture_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="y2-binary-size-") as tmp:
            report = pathlib.Path(tmp) / "sections.txt"
            report.write_text(
                "Segment __TEXT: 65536\n"
                "\tSection __text: 50000\n"
                "Segment __TEXT: 73728\n"
                "\tSection __text: 60000\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BinarySizeError, "duplicate segment __TEXT"):
                parse_macho_sections(report)

    def test_decrease_is_informational_and_named_explicitly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="y2-binary-size-") as tmp:
            root = pathlib.Path(tmp)
            base_binary = root / "base-y2"
            head_binary = root / "head-y2"
            base_binary.write_bytes(b"b" * 100)
            head_binary.write_bytes(b"h" * 90)
            base_sections = root / "base-sections.txt"
            head_sections = root / "head-sections.txt"
            base_sections.write_text("Segment __TEXT: 100\n", encoding="utf-8")
            head_sections.write_text("Segment __TEXT: 90\n", encoding="utf-8")

            report = build_report(
                base_binary=base_binary,
                head_binary=head_binary,
                base_sections=base_sections,
                head_sections=head_sections,
                base_sha="a" * 40,
                head_sha="b" * 40,
                target="aarch64-macos",
                warning_bytes=52_429,
            )

            delta = report["delta"]
            self.assertIsInstance(delta, dict)
            assert isinstance(delta, dict)
            self.assertEqual("ok", report["status"])
            self.assertEqual("decrease", delta.get("direction"))
            self.assertIn(
                "The PR binary is smaller than the base binary.",
                markdown_report(report),
            )

    def test_section_parser_requires_executable_text_segment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="y2-binary-size-") as tmp:
            report = pathlib.Path(tmp) / "sections.txt"
            report.write_text("Segment __DATA: 16384\n", encoding="utf-8")

            with self.assertRaisesRegex(BinarySizeError, "missing __TEXT segment"):
                parse_macho_sections(report)



if __name__ == "__main__":
    unittest.main()
