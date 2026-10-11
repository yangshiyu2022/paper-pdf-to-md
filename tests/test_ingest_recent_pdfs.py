import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ingest_recent_pdfs.py"
SPEC = importlib.util.spec_from_file_location("ingest_recent_pdfs", SCRIPT)
INGEST = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = INGEST
SPEC.loader.exec_module(INGEST)


def write_pdf(path: Path, lines: list[str]) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_textbox((72, 72, 540, 760), "\n".join(lines), fontsize=11)
    document.save(path)
    document.close()


class IngestRecentPdfsTests(unittest.TestCase):
    def test_dry_run_imports_only_recent_unique_research_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "Downloads"
            library = root / "Library"
            existing = library / "existing.pdf"
            duplicate = downloads / "duplicate.pdf"
            unique = downloads / "unique.pdf"
            manual = downloads / "manual.pdf"
            downloads.mkdir()
            library.mkdir()
            paper = [
                "A Study of Timber Material Mechanics",
                "Abstract: This study investigates mechanical behavior.",
                "Keywords: timber, compression",
                "DOI: 10.1234/example.2026.001",
            ]
            write_pdf(existing, paper)
            write_pdf(duplicate, [*paper, "Downloaded copy"])
            write_pdf(unique, [
                "Bamboo Composite Flexural Response",
                "Abstract: This study evaluates bending strength.",
                "Keywords: bamboo, bending",
                "DOI: 10.5678/example.2026.002",
            ])
            write_pdf(manual, ["User Manual", "Installation instructions", "Warranty information"])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = INGEST.main([
                    "--downloads", str(downloads), "--library", str(library), "--dry-run",
                ])

            report = json.loads(output.getvalue())
            statuses = {Path(item["source"]).name: item["status"] for item in report["items"]}
            self.assertEqual(exit_code, 0)
            self.assertEqual(statuses["unique.pdf"], "would-import")
            self.assertEqual(statuses["duplicate.pdf"], "duplicate-skipped")
            self.assertEqual(statuses["manual.pdf"], "left-in-downloads")
            self.assertTrue(unique.exists())
            self.assertFalse((library / INGEST.INDEX_DIR_NAME).exists())


if __name__ == "__main__":
    unittest.main()
