#!/usr/bin/env python
"""Offline-first academic PDF to Markdown converter with local OCR/formula review."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

TOOL_VERSION = "0.4.0"
FORMULA_STRATEGY_VERSION = "mineru-unimernet-v1"
SCAN_TEXT_THRESHOLD = 30
MATH_FONT_RE = re.compile(r"(Math|Symbol|CMEX|CMSY|CMMI|STIX|CambriaMath|MTExtra|LMMath)", re.I)
MATH_CHAR_RE = re.compile(r"[=+\-*/<>^_{}\\()[\]|]")
os.environ.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Force UTF-8 I/O regardless of launcher. PYTHONIOENCODING is not inherited
# through PowerShell Start-Process, and -I ignores the environment, so a CJK
# output path would raise UnicodeEncodeError on print under the GBK locale.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")


class ConversionError(RuntimeError):
    pass


def slugify_ascii(text: str, limit: int = 80) -> str:
    """Reduce a title to a conservative ASCII filename stem."""
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    ascii_only = text.encode("ascii", "ignore").decode("ascii")
    ascii_only = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_")
    ascii_only = re.sub(r"_{2,}", "_", ascii_only)
    return ascii_only[:limit].strip("_")


def english_title(pdf: Path) -> tuple[str, str]:
    """Derive an ASCII stem for a PDF, preferring its real English title.

    Returns (stem, source). MinerU embeds the input filename in its artifact
    paths and internal JSON, so an ASCII stem keeps every intermediate path
    codepage-independent regardless of the original CJK filename.
    """
    fitz = import_fitz()
    try:
        doc = fitz.open(pdf)
    except Exception:
        return slugify_ascii(pdf.stem) or "paper", "fallback-original-stem"
    try:
        meta_title = (doc.metadata or {}).get("title") or ""
        candidate = slugify_ascii(meta_title)
        # Reject metadata that is a filename, a tool artifact, or too short to be a title.
        if len(candidate) >= 20 and not re.search(r"(microsoft|word|doc\d|untitled|\.dvi|\.tex|elsevier)", candidate, re.I):
            return candidate, "pdf-metadata-title"
        if doc.page_count:
            spans: list[tuple[float, str]] = []
            page = doc[0]
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type", 0) != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        content = (span.get("text") or "").strip()
                        if content:
                            spans.append((float(span.get("size", 0)), content))
            if spans:
                # A paper title is normally the largest type on page 1.
                largest = max(size for size, _ in spans)
                title_parts = [content for size, content in spans if size >= largest - 0.5]
                candidate = slugify_ascii(" ".join(title_parts))
                if len(candidate) >= 20:
                    return candidate, "first-page-largest-font"
    except Exception:
        pass
    finally:
        doc.close()
    return slugify_ascii(pdf.stem) or "paper", "fallback-original-stem"


def mineru_executable() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    command = Path(sys.executable).resolve().parent / f"mineru{suffix}"
    if not command.exists():
        raise ConversionError(f"MinerU is not installed in the dedicated runtime: {command}")
    return command


def run_mineru(pdf: Path, review_dir: Path, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Run MinerU as the only Markdown producer and retain its native artifacts."""
    raw_dir = review_dir / "mineru"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    stage = tempfile.mkdtemp(prefix="paper_pdf_stage_")
    stem, stem_source = english_title(pdf)
    staged_pdf = Path(stage) / f"{stem}.pdf"
    shutil.copy2(pdf, staged_pdf)
    command = [
        str(mineru_executable()), "-p", str(staged_pdf), "-o", str(raw_dir),
        "--backend", "pipeline", "--method", "auto", "--lang", args.language,
        # Formula markup comes from MinerU's local UniMERNet MFR model
        # (models/MFR/unimernet_hf_small_2503).
        "--formula", "false" if args.formula == "off" else "true",
        "--table", "true",
    ]
    env = os.environ.copy()
    env["MINERU_MODEL_SOURCE"] = "local"
    # Make the child write UTF-8 so the parent decode below is codepage-independent.
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        # Decode as UTF-8 explicitly. text=True would use the system locale (GBK on
        # zh-CN Windows), and MinerU emits UTF-8 progress bars and CJK filenames that
        # crash the pipe reader thread with UnicodeDecodeError.
        result = subprocess.run(
            command, capture_output=True, timeout=args.mineru_timeout, check=False, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(f"MinerU timed out after {args.mineru_timeout}s") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()[-4000:]
        raise ConversionError(f"MinerU conversion failed ({result.returncode}): {message}")
    markdown_files = sorted(raw_dir.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not markdown_files:
        raise ConversionError("MinerU completed without producing Markdown.")
    source = markdown_files[0]
    text = source.read_text(encoding="utf-8")
    relative_source_dir = relpath(source.parent, output_base := review_dir.parent)
    text = re.sub(r"\]\((?![a-zA-Z][a-zA-Z0-9+.-]*:|/)([^)]+)\)", lambda m: f"]({relative_source_dir}/{m.group(1)})", text)
    return text, {
        "version": "3.4.4",
        "command": command,
        "artifact_dir": str(raw_dir),
        "source_markdown": str(source),
        "formula_model": None if args.formula == "off" else "unimernet_hf_small_2503",
        "ascii_stem": stem,
        "ascii_stem_source": stem_source,
        "stdout_tail": (result.stdout or "")[-2000:],
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=str(path.parent), delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def file_lock(path: Path, timeout: float) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            break
        except FileExistsError:
            if time.time() > deadline:
                raise ConversionError(f"Timed out waiting for lock: {path}")
            time.sleep(0.25)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def require_local_pdf(raw: Path) -> Path:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", str(raw)):
        raise ConversionError("Only local PDF paths are accepted; URLs are forbidden.")
    path = raw.expanduser().resolve()
    if not path.exists():
        raise ConversionError(f"PDF does not exist: {path}")
    if path.suffix.lower() != ".pdf":
        raise ConversionError(f"Input must be a .pdf file: {path}")
    return path


def import_fitz():
    try:
        return importlib.import_module("fitz")
    except Exception as exc:
        raise ConversionError("PyMuPDF is required in the dedicated runtime.") from exc


def page_text_chars(page: Any) -> int:
    return len((page.get_text("text") or "").strip())


def page_blocks(page: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        lines: list[str] = []
        fonts: list[str] = []
        for line in block.get("lines", []):
            parts: list[str] = []
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text:
                    parts.append(text)
                    fonts.append(span.get("font", ""))
            if parts:
                lines.append("".join(parts).strip())
        text = "\n".join(line for line in lines if line).strip()
        if text:
            blocks.append({"bbox": block.get("bbox", [0, 0, 0, 0]), "text": text, "fonts": fonts})
    blocks.sort(key=lambda b: (round(float(b["bbox"][1]), 1), round(float(b["bbox"][0]), 1)))
    return blocks


def formula_candidate(block: dict[str, Any], page_width: float) -> tuple[bool, str]:
    text = (block.get("text") or "").strip()
    if not text:
        return False, "empty"
    compact = re.sub(r"\s+", "", text)
    fonts = " ".join(block.get("fonts") or [])
    x0, _y0, x1, _y1 = [float(v) for v in block.get("bbox", [0, 0, 0, 0])]
    centered = abs(((x0 + x1) / 2) - (page_width / 2)) < page_width * 0.22
    width = max(x1 - x0, 1)
    math_chars = len(MATH_CHAR_RE.findall(compact))
    density = math_chars / max(len(compact), 1)
    sparse_words = len(re.findall(r"[A-Za-z]{4,}", text)) <= 3
    eqno_tail = bool(re.search(r"\(\s*\d+[A-Za-z]?\s*\)\s*$", compact))
    if MATH_FONT_RE.search(fonts) and (density > 0.08 or centered or eqno_tail):
        return True, "math-font"
    if eqno_tail and sparse_words and (density > 0.06 or centered):
        return True, "equation-number"
    if centered and sparse_words and density > 0.18 and width > page_width * 0.18:
        return True, "centered-symbolic"
    return False, "not-formula"


def expand_rect(fitz: Any, bbox: list[float], page_rect: Any, pad: float) -> Any:
    rect = fitz.Rect(*bbox)
    rect.x0 = max(page_rect.x0, rect.x0 - pad)
    rect.y0 = max(page_rect.y0, rect.y0 - pad)
    rect.x1 = min(page_rect.x1, rect.x1 + pad)
    rect.y1 = min(page_rect.y1, rect.y1 + pad)
    return rect


def load_rapidocr() -> tuple[Any, dict[str, Any]]:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        import rapidocr_onnxruntime  # type: ignore
    except Exception as exc:
        raise ConversionError("RapidOCR is required for scanned-page OCR. Use --ocr off for native PDFs if unavailable.") from exc
    return RapidOCR(), {"package": "rapidocr-onnxruntime", "version": getattr(rapidocr_onnxruntime, "__version__", "unknown")}


def render_page(page: Any, out: Path, dpi: int) -> None:
    fitz = import_fitz()
    out.parent.mkdir(parents=True, exist_ok=True)
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
    pix.save(str(out))


def ocr_page(engine: Any, image: Path) -> str:
    result, _elapsed = engine(str(image))
    return "\n".join(str(item[1]).strip() for item in (result or []) if len(item) >= 2 and str(item[1]).strip())


def clean_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def inspect(pdf: Path) -> dict[str, Any]:
    fitz = import_fitz()
    doc = fitz.open(pdf)
    pages = []
    for index, page in enumerate(doc, start=1):
        reasons: dict[str, int] = {}
        for block in page_blocks(page):
            ok, reason = formula_candidate(block, page.rect.width)
            if ok:
                reasons[reason] = reasons.get(reason, 0) + 1
        chars = page_text_chars(page)
        pages.append({
            "page": index,
            "native_text_chars": chars,
            "scan_like": chars < SCAN_TEXT_THRESHOLD,
            "text_blocks": len(page_blocks(page)),
            "formula_candidates": sum(reasons.values()),
            "formula_candidate_reasons": reasons,
        })
    return {"pdf": str(pdf), "sha256": sha256_file(pdf), "pages": len(doc), "page_summary": pages}


def cache_key(pdf: Path, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "tool": "paper-pdf-local-ocr",
        "version": TOOL_VERSION,
        "pdf_sha256": sha256_file(pdf),
        "options": {
            "ocr": args.ocr,
            "formula": args.formula,
            "dpi": args.dpi,
            "formula_scale": args.formula_scale,
            "language": args.language,
            "formula_strategy": FORMULA_STRATEGY_VERSION,
        },
    }


def cache_valid(sidecar: Path, output: Path, key: dict[str, Any]) -> bool:
    if not sidecar.exists() or not output.exists():
        return False
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("cache_key") == key and data.get("output", {}).get("sha256") == sha256_file(output)


def json_safe_options(args: argparse.Namespace) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in vars(args).items():
        if isinstance(value, Path):
            safe[key] = str(value)
        else:
            safe[key] = value
    return safe


def convert(pdf: Path, output: Path, args: argparse.Namespace) -> dict[str, Any]:
    fitz = import_fitz()
    doc = fitz.open(pdf)
    review_dir = Path(args.review_dir).resolve() if args.review_dir else output.with_suffix("").with_name(output.stem + "_review")
    text_crops_dir = review_dir / "text_crops"
    page_images_dir = review_dir / "page_images"
    # MinerU owns the deliverable Markdown, including formula LaTeX via its local
    # UniMERNet MFR model. The loop below only flags pages/blocks a human should
    # eyeball; it never produces competing formula markup.
    mineru_markdown, mineru_meta = run_mineru(pdf, review_dir, args)
    pages: list[dict[str, Any]] = []
    text_reviews: list[dict[str, Any]] = []
    ocr_engine = ocr_meta = None
    if args.ocr == "always":
        ocr_engine, ocr_meta = load_rapidocr()

    for page_no, page in enumerate(doc, start=1):
        chars = page_text_chars(page)
        scan_like = chars < SCAN_TEXT_THRESHOLD
        use_ocr = args.ocr == "always" or (args.ocr == "auto" and scan_like)
        # Do not append native extraction to the MinerU Markdown.
        if use_ocr:
            if ocr_engine is None:
                ocr_engine, ocr_meta = load_rapidocr()
            page_image = page_images_dir / f"page_{page_no:04d}.png"
            render_page(page, page_image, args.dpi)
            review_text = ocr_page(ocr_engine, page_image)
            text_reviews.append({"page": page_no, "image": relpath(page_image, output.parent), "ocr_text": review_text, "review_required": True, "reason": "scan-like-page"})
            pages.append({"page": page_no, "mode": "review-only-local-ocr", "native_text_chars": chars, "scan_like": scan_like})
            continue
        formula_candidates = 0
        for block in page_blocks(page):
            text = block["text"]
            text_ambiguous = "\ufffd" in text or bool(re.search(r"\?{2,}", text))
            if text_ambiguous:
                tid = f"t{page_no:04d}_{len(text_reviews)+1:04d}"
                rect = expand_rect(fitz, block["bbox"], page.rect, 5.0)
                crop = text_crops_dir / f"{tid}.png"
                crop.parent.mkdir(parents=True, exist_ok=True)
                pix = page.get_pixmap(matrix=fitz.Matrix(args.formula_scale, args.formula_scale), clip=rect, alpha=False)
                pix.save(str(crop))
                ocr_text = ""
                if args.ocr != "off":
                    if ocr_engine is None:
                        ocr_engine, ocr_meta = load_rapidocr()
                    ocr_text = ocr_page(ocr_engine, crop)
                text_reviews.append({"id": tid, "page": page_no, "bbox": [round(float(v), 3) for v in rect], "image": relpath(crop, output.parent), "native_text": text, "ocr_text": ocr_text, "review_required": True, "reason": "replacement-glyph-or-repeated-question-mark"})
            is_formula, _reason = formula_candidate(block, page.rect.width)
            if is_formula:
                formula_candidates += 1
        pages.append({"page": page_no, "mode": "native-text", "native_text_chars": chars, "scan_like": scan_like, "formula_candidates": formula_candidates})

    output.parent.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output, mineru_markdown.rstrip() + "\n")
    text_review_json = review_dir / "text_review.json"
    atomic_write_json(text_review_json, {"pages": text_reviews})
    sidecar = output.with_suffix(output.suffix + ".paper-pdf-local-ocr.json")
    obj = {
        "tool": "paper-pdf-local-ocr",
        "version": TOOL_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cache_key": cache_key(pdf, args),
        "input": {"path": str(pdf), "sha256": sha256_file(pdf), "bytes": pdf.stat().st_size},
        "output": {"path": str(output), "sha256": sha256_file(output), "bytes": output.stat().st_size},
        "review": {"dir": str(review_dir), "text_json": str(text_review_json), "text_review_count": len(text_reviews), "formula_candidates": sum(p.get("formula_candidates", 0) for p in pages)},
        "options": json_safe_options(args),
        "pages": pages,
        "backend": {"mineru": mineru_meta, "ocr_review": ocr_meta},
        "limits": ["MinerU is the sole Markdown producer.", "Formula LaTeX comes from MinerU's UniMERNet MFR model and has no calibrated confidence score.", "Formulas that matter to a downstream claim require human review against the source PDF.", "This CLI performs no remote OCR or API upload."],
    }
    atomic_write_json(sidecar, obj)
    return obj


def relpath(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MinerU paper PDF to Markdown with local OCR and formula review.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--ocr", choices=["off", "auto", "always"], default="auto")
    parser.add_argument("--formula", choices=["off", "auto", "always"], default="auto",
                        help="Enable MinerU's UniMERNet formula recognition. off disables it.")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--formula-scale", type=float, default=4.0,
                        help="Render scale for review crops of ambiguous text blocks.")
    parser.add_argument("--language", default="en")
    parser.add_argument("--review-dir")
    parser.add_argument("--offline", action="store_true", default=True, help="Offline mode is always enforced.")
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=120.0)
    parser.add_argument("--mineru-timeout", type=float, default=3600.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        pdf = require_local_pdf(args.pdf)
        output = args.output.expanduser().resolve()
        if args.inspect_only:
            print(json.dumps(inspect(pdf), ensure_ascii=False, indent=2))
            return 0
        sidecar = output.with_suffix(output.suffix + ".paper-pdf-local-ocr.json")
        with file_lock(output.with_suffix(output.suffix + ".lock"), args.lock_timeout):
            key = cache_key(pdf, args)
            if not args.force and cache_valid(sidecar, output, key):
                print(json.dumps({"status": "cache-hit", "output": str(output), "sidecar": str(sidecar)}, ensure_ascii=False))
                return 0
            result = convert(pdf, output, args)
            print(json.dumps({"status": "converted", "output": str(output), "sidecar": str(sidecar), "review": result["review"]}, ensure_ascii=False))
            return 0
    except ConversionError as exc:
        print(f"paper-pdf-local-ocr: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
