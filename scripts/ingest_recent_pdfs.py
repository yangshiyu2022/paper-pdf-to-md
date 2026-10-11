#!/usr/bin/env python
"""Ingest likely research PDFs downloaded during the last N hours.

The job is deliberately conservative: duplicates and uncertain PDFs stay in
Downloads, while a unique PDF is moved only after its Markdown conversion
finishes successfully.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
AUTO_SCRIPT = HERE / "convert_paper_pdf_auto.py"
DEFAULT_LIBRARY = os.environ.get("PAPER_LIBRARY_ROOT")
INDEX_DIR_NAME = "_paper_pdf_to_md"
INGEST_DIR_NAME = "09_待规范命名"
MD_DIR_NAME = "09_待规范命名MD"

RESEARCH_SIGNALS = (
    "abstract", "introduction", "materials and methods", "methods", "results",
    "discussion", "conclusion", "keywords", "doi", "journal", "volume",
    "issn", "received", "published", "university", "et al.", "references",
    "the aim", "this study", "研究", "摘要", "引言", "材料与方法", "结果",
    "讨论", "结论", "关键词", "参考文献",
)
NON_RESEARCH_SIGNALS = (
    "invoice", "receipt", "manual", "datasheet", "brochure", "catalog",
    "curriculum vitae", "resume", "application form", "bank statement",
    "menu", "timetable", "safety data sheet", "terms and conditions",
)
GENERIC_STEMS = {"main", "article", "paper", "document", "download", "file", "full"}


@dataclass
class PdfInfo:
    path: str
    size: int
    mtime_ns: int
    sha256: str
    doi: str | None
    title_key: str | None
    stem_key: str | None


def default_downloads() -> Path:
    """Resolve the Windows Downloads known folder, including redirected paths."""
    with contextlib.suppress(Exception):
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            raw, _kind = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            candidate = Path(os.path.expandvars(str(raw))).expanduser()
            if candidate.exists():
                return candidate
    return Path.home() / "Downloads"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_key(value: str, limit: int = 180) -> str:
    value = value.lower().replace("–", "-").replace("—", "-")
    value = re.sub(r"https?://doi\.org/", "", value)
    value = re.sub(r"doi\s*:\s*", "", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def extract_pdf_text(path: Path) -> tuple[str, str | None]:
    """Read a small prefix for classification and stable DOI/title keys."""
    try:
        import fitz  # type: ignore
    except Exception:
        return "", None
    try:
        with fitz.open(path) as doc:
            metadata_title = (doc.metadata or {}).get("title") or ""
            pages = min(3, len(doc))
            text = "\n".join(doc.load_page(i).get_text("text") for i in range(pages))
            return text, metadata_title.strip() or None
    except Exception:
        return "", None


def find_doi(text: str) -> str | None:
    match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", text, flags=re.IGNORECASE)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)").lower()
    return doi if len(doi) > 12 else None


def title_key(text: str, metadata_title: str | None) -> str | None:
    if metadata_title and len(metadata_title) >= 15:
        return normalize_key(metadata_title)
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if len(line) < 20 or len(line) > 240:
            continue
        low = line.lower()
        if any(token in low for token in ("abstract", "introduction", "doi", "issn", "university")):
            continue
        if sum(ch.isalpha() for ch in line) >= 12:
            return normalize_key(line)
    return None


def stem_key(path: Path) -> str | None:
    stem = normalize_key(path.stem)
    if not stem or stem in GENERIC_STEMS or len(stem) < 8:
        return None
    return stem


def classify(path: Path, text: str) -> tuple[str, int, list[str]]:
    haystack = f"{path.stem.lower()}\n{text.lower()}"
    hits = [signal for signal in RESEARCH_SIGNALS if signal in haystack]
    rejected = [signal for signal in NON_RESEARCH_SIGNALS if signal in haystack]
    unique_hits = list(dict.fromkeys(hits))
    has_doi = find_doi(haystack) is not None
    thesis = any(term in haystack for term in ("dissertation", "thesis", "博士学位论文", "硕士学位论文", "博士论文", "硕士论文"))
    english_abstract = "abstract" in haystack and any(term in haystack for term in ("keywords", "references", "materials and methods", "results"))
    chinese_abstract = "摘要" in haystack and any(term in haystack for term in ("关键词", "参考文献", "材料与方法", "结果与讨论"))
    book_cues = any(term in haystack for term in ("isbn", "专著", "monograph")) and any(
        term in haystack for term in ("参考文献", "references", "研究", "science", "university")
    )
    if rejected and not (has_doi or thesis or english_abstract or chinese_abstract):
        return "non_research", len(unique_hits), unique_hits
    if has_doi or thesis or english_abstract or chinese_abstract or book_cues:
        return "research_candidate", len(unique_hits), unique_hits
    return "uncertain", len(unique_hits), unique_hits


def observed_timestamp(path: Path) -> float:
    stat = path.stat()
    return max(stat.st_mtime, getattr(stat, "st_ctime", 0.0))


def load_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("files", {}) if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_index(path: Path, entries: Iterable[PdfInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "files": {entry.path: asdict(entry) for entry in entries},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_library_index(library: Path, index_path: Path, persist: bool = True) -> list[PdfInfo]:
    cached = load_index(index_path)
    entries: list[PdfInfo] = []
    for path in library.rglob("*.pdf"):
        if INDEX_DIR_NAME in path.parts:
            continue
        key = str(path.resolve())
        stat = path.stat()
        old = cached.get(key)
        if old and old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns:
            entries.append(PdfInfo(**old))
            continue
        text, metadata_title = extract_pdf_text(path)
        entries.append(PdfInfo(
            path=key,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=sha256_file(path),
            doi=find_doi(text),
            title_key=title_key(text, metadata_title),
            stem_key=stem_key(path),
        ))
    if persist:
        save_index(index_path, entries)
    return entries


def unique_destination(folder: Path, name: str, digest: str) -> Path:
    candidate = folder / name
    if not candidate.exists():
        return candidate
    if sha256_file(candidate) == digest:
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for number in range(2, 10000):
        option = folder / f"{stem} ({number}){suffix}"
        if not option.exists():
            return option
    raise RuntimeError(f"Could not allocate a destination for {candidate.name}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest likely research PDFs downloaded recently.")
    parser.add_argument("--downloads", type=Path, default=None, help="Downloads folder; defaults to the Windows known folder.")
    parser.add_argument("--library", type=Path, default=Path(DEFAULT_LIBRARY) if DEFAULT_LIBRARY else None)
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--language", default="en")
    parser.add_argument("--cloud", action="store_true", help="Allow the API-first router to upload eligible PDFs.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--copy-source", action="store_true", help="Copy successful imports instead of moving them.")
    parser.add_argument("--min-signals", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    downloads = (args.downloads or default_downloads()).expanduser().resolve()
    if args.library is None:
        print(json.dumps({"status": "error", "error": "Pass --library or set PAPER_LIBRARY_ROOT."}, ensure_ascii=False))
        return 2
    library = args.library.expanduser().resolve()
    if not downloads.exists():
        print(json.dumps({"status": "error", "error": f"Downloads folder does not exist: {downloads}"}, ensure_ascii=False))
        return 2
    if not AUTO_SCRIPT.exists():
        print(json.dumps({"status": "error", "error": f"Missing converter: {AUTO_SCRIPT}"}, ensure_ascii=False))
        return 2

    ingest_dir = library / INGEST_DIR_NAME
    md_dir = library / MD_DIR_NAME
    state_dir = library / INDEX_DIR_NAME
    index_path = state_dir / "library-index.json"
    log_path = state_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    cutoff = time.time() - args.window_hours * 3600
    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "downloads": str(downloads), "library": str(library),
        "window_hours": args.window_hours, "cutoff_local": datetime.fromtimestamp(cutoff).isoformat(),
        "cloud": args.cloud, "dry_run": args.dry_run, "items": [],
    }

    library_entries = build_library_index(library, index_path, persist=not args.dry_run)
    known_hashes = {entry.sha256 for entry in library_entries}
    known_dois = {entry.doi for entry in library_entries if entry.doi}
    known_titles = {entry.title_key for entry in library_entries if entry.title_key and len(entry.title_key) >= 20}
    known_stems = {entry.stem_key for entry in library_entries if entry.stem_key}
    candidates = [p for p in downloads.rglob("*.pdf") if p.is_file() and observed_timestamp(p) >= cutoff]
    candidates.sort(key=observed_timestamp)

    for source in candidates:
        item: dict[str, Any] = {"source": str(source), "status": "pending"}
        try:
            text, metadata_title = extract_pdf_text(source)
            classification, score, signals = classify(source, text)
            item.update({"classification": classification, "signal_score": score, "signals": signals})
            if classification != "research_candidate" or score < args.min_signals:
                item["status"] = "left-in-downloads"
                item["reason"] = "uncertain-or-non-research"
                summary["items"].append(item)
                continue
            digest = sha256_file(source)
            doi = find_doi(text)
            tkey = title_key(text, metadata_title)
            skey = stem_key(source)
            duplicate_reason = None
            if digest in known_hashes:
                duplicate_reason = "sha256"
            elif doi and doi in known_dois:
                duplicate_reason = "doi"
            elif tkey and len(tkey) >= 20 and tkey in known_titles:
                item.update({"status": "duplicate-suspected", "reason": "title", "sha256": digest, "doi": doi})
                summary["items"].append(item)
                continue
            elif skey and skey in known_stems:
                item.update({"status": "duplicate-suspected", "reason": "filename", "sha256": digest, "doi": doi})
                summary["items"].append(item)
                continue
            if duplicate_reason:
                item.update({"status": "duplicate-skipped", "reason": duplicate_reason, "sha256": digest, "doi": doi})
                summary["items"].append(item)
                continue

            safe_name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", source.name).strip(" .") or "paper.pdf"
            destination = unique_destination(ingest_dir, safe_name, digest)
            md_path = md_dir / f"{destination.stem}.md"
            review_dir = md_dir / f"{destination.stem}_review"
            item.update({"status": "converting", "destination": str(destination), "markdown": str(md_path), "sha256": digest, "doi": doi})
            if args.dry_run:
                item["status"] = "would-import"
                summary["items"].append(item)
                known_hashes.add(digest)
                if doi: known_dois.add(doi)
                if tkey: known_titles.add(tkey)
                if skey: known_stems.add(skey)
                continue

            md_dir.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, "-I", str(AUTO_SCRIPT), str(source), "-o", str(md_path),
                       "--review-dir", str(review_dir), "--language", args.language]
            if not args.cloud:
                command.append("--force-local")
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            item["converter_exit_code"] = proc.returncode
            item["converter_stderr"] = (proc.stderr or "")[-2000:]
            if proc.returncode != 0 or not md_path.exists() or md_path.stat().st_size == 0:
                item["status"] = "conversion-failed"
                summary["items"].append(item)
                continue
            ingest_dir.mkdir(parents=True, exist_ok=True)
            if args.copy_source:
                shutil.copy2(source, destination)
            else:
                shutil.move(str(source), str(destination))
            item["status"] = "imported"
            known_hashes.add(digest)
            if doi: known_dois.add(doi)
            if tkey: known_titles.add(tkey)
            if skey: known_stems.add(skey)
        except Exception as exc:
            item.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        summary["items"].append(item)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["counts"] = {
        status: sum(1 for item in summary["items"] if item.get("status") == status)
        for status in sorted({item.get("status") for item in summary["items"]})
    }
    if not args.dry_run:
        state_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not any(item.get("status") == "error" for item in summary["items"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
