#!/usr/bin/env python
"""Academic PDF to Markdown via the MinerU cloud API.

This uploads the PDF to mineru.net. Use paper-pdf-local-ocr for anything that
must not leave the machine.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

TOOL_VERSION = "0.1.0"
API_BASE = "https://mineru.net/api/v4"
SCAN_TEXT_THRESHOLD = 30
EXPIRY_WARN_DAYS = 14

# The token may live under either agent's home. Env var always wins.
SECRETS_CANDIDATES = (
    Path.home() / ".codex" / "secrets" / "mineru-api.json",
    Path.home() / ".claude" / "secrets" / "mineru-api.json",
)
SECRETS_PATH = SECRETS_CANDIDATES[0]

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_QUOTA = 3  # Distinct so a router can fall back to the local backend.

# Substrings that mark quota/rate exhaustion rather than a generic failure.
QUOTA_MARKERS = (
    "quota", "配额", "额度", "rate limit", "too many requests",
    "exceeded", "limit exceeded", "insufficient",
)


# Force UTF-8 I/O regardless of launcher. PYTHONIOENCODING is not inherited
# through PowerShell Start-Process, and -I ignores the environment, so a CJK
# output path would raise UnicodeEncodeError on print under the GBK locale.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")


class ConversionError(RuntimeError):
    pass


class QuotaExhausted(ConversionError):
    """Raised when the API refuses work because the allowance is spent."""


def looks_like_quota(text: str) -> bool:
    """Heuristic: does this error text indicate a spent allowance?

    The exact wording MinerU returns on daily-quota exhaustion has not been
    observed, so this matches a family of common markers. A false negative is
    harmless for a router that falls back on any API failure.
    """
    low = (text or "").lower()
    return any(marker in low for marker in QUOTA_MARKERS)


def load_token() -> tuple[str, str | None]:
    """Read the API token from the environment or the secrets file.

    Returns (token, expires_on). The token is never written to the sidecar.
    """
    env_token = os.environ.get("MINERU_API_TOKEN", "").strip()
    secrets_file = next((p for p in SECRETS_CANDIDATES if p.exists()), None)
    data: dict[str, Any] = {}
    if secrets_file is not None:
        try:
            data = json.loads(secrets_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConversionError(f"Could not parse {secrets_file}: {exc}") from exc
    token = env_token or (data.get("token") or "").strip()
    expires = data.get("expires_on")
    if not token:
        locations = " or ".join(str(p) for p in SECRETS_CANDIDATES)
        raise ConversionError(
            "No MinerU API token. Register at https://mineru.net/ to obtain one, "
            "then set MINERU_API_TOKEN or create "
            f"{locations} with {{\"token\": \"sk-...\", \"expires_on\": \"YYYY-MM-DD\"}}. "
            "Without a token, use the local MinerU converter."
        )
    return token, expires


def expiry_warning(expires_on: str | None) -> str | None:
    """Warn before the token dies, so an auth failure is never a surprise."""
    if not expires_on:
        return None
    try:
        due = datetime.strptime(expires_on, "%Y-%m-%d").date()
    except ValueError:
        return None
    left = (due - date.today()).days
    if left < 0:
        return f"MinerU API token EXPIRED on {expires_on} ({-left} days ago). Renew it at mineru.net."
    if left <= EXPIRY_WARN_DAYS:
        return f"MinerU API token expires on {expires_on} (in {left} days). Renew or replace it soon."
    return None


def api_call(url: str, token: str, method: str = "GET", body: dict | None = None, timeout: float = 120.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 429 or looks_like_quota(detail):
            raise QuotaExhausted(f"MinerU API quota or rate limit reached (HTTP {exc.code}): {detail}") from exc
        if exc.code in (401, 403):
            raise ConversionError(
                f"MinerU API rejected the token (HTTP {exc.code}). If the token is past its "
                f"expiry date, renew it at mineru.net. Response: {detail}"
            ) from exc
        raise ConversionError(f"MinerU API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ConversionError(f"Could not reach the MinerU API: {exc.reason}") from exc
    if payload.get("code") != 0:
        message = f"code={payload.get('code')} msg={payload.get('msg')}"
        if looks_like_quota(str(payload.get("msg", ""))):
            raise QuotaExhausted(f"MinerU API quota exhausted: {message}")
        raise ConversionError(f"MinerU API error {message}")
    return payload["data"]


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
        import importlib

        return importlib.import_module("fitz")
    except Exception as exc:
        raise ConversionError("PyMuPDF is required.") from exc


def slugify_ascii(text: str, limit: int = 80) -> str:
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    out = text.encode("ascii", "ignore").decode("ascii")
    out = re.sub(r"[^A-Za-z0-9]+", "_", out).strip("_")
    return re.sub(r"_{2,}", "_", out)[:limit].strip("_")


def english_title(pdf: Path) -> tuple[str, str]:
    """Derive an ASCII stem, preferring the paper's real English title.

    The API echoes the submitted filename back in its status rows, and a CJK
    name comes back mojibaked. An ASCII stem keeps the round trip readable.
    """
    fitz = import_fitz()
    try:
        doc = fitz.open(pdf)
    except Exception:
        return slugify_ascii(pdf.stem) or "paper", "fallback-original-stem"
    try:
        candidate = slugify_ascii((doc.metadata or {}).get("title") or "")
        if len(candidate) >= 20 and not re.search(r"(microsoft|word|doc\d|untitled|\.dvi|\.tex|elsevier)", candidate, re.I):
            return candidate, "pdf-metadata-title"
        if doc.page_count:
            spans: list[tuple[float, str]] = []
            for block in doc[0].get_text("dict").get("blocks", []):
                if block.get("type", 0) != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        content = (span.get("text") or "").strip()
                        if content:
                            spans.append((float(span.get("size", 0)), content))
            if spans:
                largest = max(size for size, _ in spans)
                candidate = slugify_ascii(" ".join(c for s, c in spans if s >= largest - 0.5))
                if len(candidate) >= 20:
                    return candidate, "first-page-largest-font"
    except Exception:
        pass
    finally:
        doc.close()
    return slugify_ascii(pdf.stem) or "paper", "fallback-original-stem"


def page_stats(pdf: Path) -> list[dict[str, Any]]:
    """Local page inspection so scan-like pages can be flagged for review."""
    fitz = import_fitz()
    doc = fitz.open(pdf)
    out = []
    for index, page in enumerate(doc, start=1):
        chars = len((page.get_text("text") or "").strip())
        out.append({"page": index, "native_text_chars": chars, "scan_like": chars < SCAN_TEXT_THRESHOLD})
    doc.close()
    return out


def request_upload_url(token: str, filename: str, args: argparse.Namespace) -> tuple[str, str]:
    data = api_call(f"{API_BASE}/file-urls/batch", token, "POST", {
        "enable_formula": args.formula != "off",
        "enable_table": args.table != "off",
        "language": args.language,
        "files": [{"name": filename, "is_ocr": args.ocr != "off"}],
    }, timeout=args.request_timeout)
    urls = data.get("file_urls") or []
    if not data.get("batch_id") or not urls:
        raise ConversionError(f"API did not return a batch_id and upload URL: {data}")
    return data["batch_id"], urls[0]


def upload_pdf(put_url: str, pdf: Path, timeout: float) -> None:
    """PUT the bytes to the presigned URL.

    The OSS presigned signature does not cover Content-Type, so urllib's default
    (application/x-www-form-urlencoded for data=) triggers SignatureDoesNotMatch.
    Send an empty Content-Type instead.
    """
    raw = pdf.read_bytes()
    req = urllib.request.Request(put_url, data=raw, method="PUT")
    req.add_unredirected_header("Content-Type", "")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201, 204):
                raise ConversionError(f"Upload returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ConversionError(f"Upload failed HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ConversionError(f"Upload could not reach the storage endpoint: {exc.reason}") from exc


def poll_batch(token: str, batch_id: str, args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.time() + args.api_timeout
    last_state = None
    while time.time() < deadline:
        data = api_call(f"{API_BASE}/extract-results/batch/{batch_id}", token, timeout=args.request_timeout)
        rows = data.get("extract_result") or []
        if rows:
            row = rows[0]
            state = row.get("state")
            if state != last_state:
                progress = row.get("extract_progress") or {}
                extracted, total = progress.get("extracted_pages"), progress.get("total_pages")
                suffix = f" ({extracted}/{total} pages)" if extracted is not None else ""
                print(f"paper-pdf-api-ocr: state={state}{suffix}", file=sys.stderr)
                last_state = state
            if state == "done":
                if not row.get("full_zip_url"):
                    raise ConversionError(f"API reported done without a result URL: {row}")
                return row
            if state in ("failed", "error"):
                raise ConversionError(f"API conversion failed: {row.get('err_msg') or row}")
        time.sleep(args.poll_interval)
    raise ConversionError(f"API did not finish within {args.api_timeout}s (batch_id={batch_id})")


def fetch_results(zip_url: str, review_dir: Path, output_parent: Path, timeout: float) -> tuple[str, dict[str, Any]]:
    """Download the result archive, extract it, and return the Markdown."""
    raw_dir = review_dir / "mineru_api"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(zip_url, timeout=timeout) as resp:
            blob = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ConversionError(f"Could not download the result archive: {exc}") from exc
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ConversionError("Result archive is not a valid ZIP.") from exc

    resolved_root = raw_dir.resolve()
    for member in archive.namelist():
        # Refuse absolute paths and traversal before writing anything.
        if member.startswith(("/", "\\")) or ".." in Path(member).parts:
            raise ConversionError(f"Refusing unsafe archive member: {member}")
        target = (resolved_root / member).resolve()
        if not str(target).startswith(str(resolved_root)):
            raise ConversionError(f"Archive member escapes the review directory: {member}")
    archive.extractall(raw_dir)

    markdown_files = sorted(raw_dir.rglob("*.md"), key=lambda p: p.stat().st_size, reverse=True)
    if not markdown_files:
        raise ConversionError("Result archive contained no Markdown.")
    source = markdown_files[0]
    text = source.read_text(encoding="utf-8")
    rel = relpath(source.parent, output_parent)
    text = re.sub(r"\]\((?![a-zA-Z][a-zA-Z0-9+.-]*:|/)([^)]+)\)", lambda m: f"]({rel}/{m.group(1)})", text)
    return text, {
        "zip_url": zip_url,
        "zip_bytes": len(blob),
        "archive_entries": len(archive.namelist()),
        "artifact_dir": str(raw_dir),
        "source_markdown": str(source),
    }


def relpath(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def cache_key(pdf: Path, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "tool": "paper-pdf-api-ocr",
        "version": TOOL_VERSION,
        "pdf_sha256": sha256_file(pdf),
        "options": {"ocr": args.ocr, "formula": args.formula, "table": args.table, "language": args.language},
    }


def cache_valid(sidecar: Path, output: Path, key: dict[str, Any]) -> bool:
    if not sidecar.exists() or not output.exists():
        return False
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("cache_key") == key and data.get("output", {}).get("sha256") == sha256_file(output)


def convert(pdf: Path, output: Path, args: argparse.Namespace) -> dict[str, Any]:
    token, expires_on = load_token()
    warning = expiry_warning(expires_on)
    if warning:
        print(f"paper-pdf-api-ocr: WARNING: {warning}", file=sys.stderr)

    review_dir = Path(args.review_dir).resolve() if args.review_dir else output.with_suffix("").with_name(output.stem + "_review")
    review_dir.mkdir(parents=True, exist_ok=True)
    stem, stem_source = english_title(pdf)
    pages = page_stats(pdf)
    started = time.time()
    batch_id, put_url = request_upload_url(token, f"{stem}.pdf", args)
    upload_pdf(put_url, pdf, args.upload_timeout)
    row = poll_batch(token, batch_id, args)
    markdown, meta = fetch_results(row["full_zip_url"], review_dir, output.parent, args.download_timeout)
    elapsed = time.time() - started

    # The API can return state=done with only the first page parsed. Compare the
    # remote Markdown against locally extracted text before trusting it.
    native_chars = sum(p["native_text_chars"] for p in pages)
    if native_chars > 5000 and len(markdown) < native_chars * 0.25:
        raise ConversionError(
            f"API returned suspiciously short Markdown ({len(markdown)} chars) for a PDF with "
            f"{native_chars} native text chars across {len(pages)} pages. Treating as a partial "
            f"conversion; rerun to retry (batch_id={batch_id})."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output, markdown.rstrip() + "\n")
    text_review = [
        {"page": p["page"], "review_required": True, "reason": "scan-like-page"}
        for p in pages if p["scan_like"]
    ]
    text_review_json = review_dir / "text_review.json"
    atomic_write_json(text_review_json, {"pages": text_review})

    sidecar = output.with_suffix(output.suffix + ".paper-pdf-api-ocr.json")
    obj = {
        "tool": "paper-pdf-api-ocr",
        "version": TOOL_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cache_key": cache_key(pdf, args),
        "input": {"path": str(pdf), "sha256": sha256_file(pdf), "bytes": pdf.stat().st_size},
        "output": {"path": str(output), "sha256": sha256_file(output), "bytes": output.stat().st_size},
        "review": {"dir": str(review_dir), "text_json": str(text_review_json), "scan_like_pages": len(text_review)},
        "options": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "pages": pages,
        "backend": {
            "service": "mineru.net api/v4",
            "batch_id": batch_id,
            "submitted_filename": f"{stem}.pdf",
            "ascii_stem_source": stem_source,
            "token_expires_on": expires_on,
            "elapsed_seconds": round(elapsed, 1),
            **meta,
        },
        "limits": [
            "This tool UPLOADS the PDF to mineru.net; it is not a local-only path.",
            "Markdown, tables and formula LaTeX come from a remote model with no calibrated confidence score.",
            "Formulas that matter to a downstream claim require human review against the source PDF.",
            "Use paper-pdf-local-ocr for documents that must not leave this machine.",
        ],
    }
    if warning:
        obj["warnings"] = [warning]
    atomic_write_json(sidecar, obj)
    return obj


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert an academic PDF to Markdown via the MinerU cloud API.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--ocr", choices=["off", "auto", "always"], default="auto")
    parser.add_argument("--formula", choices=["off", "auto", "always"], default="auto")
    parser.add_argument("--table", choices=["off", "auto", "always"], default="auto")
    parser.add_argument("--language", default="en")
    parser.add_argument("--review-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check-token", action="store_true", help="Report token expiry status and exit.")
    parser.add_argument("--lock-timeout", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--upload-timeout", type=float, default=900.0)
    parser.add_argument("--download-timeout", type=float, default=900.0)
    parser.add_argument("--api-timeout", type=float, default=1800.0, help="Max seconds to wait for remote conversion.")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.check_token:
            _token, expires_on = load_token()
            warning = expiry_warning(expires_on)
            left = None
            if expires_on:
                with contextlib.suppress(ValueError):
                    left = (datetime.strptime(expires_on, "%Y-%m-%d").date() - date.today()).days
            print(json.dumps({"token": "present", "expires_on": expires_on, "days_left": left, "warning": warning}, ensure_ascii=False))
            return 0
        pdf = require_local_pdf(args.pdf)
        output = args.output.expanduser().resolve()
        sidecar = output.with_suffix(output.suffix + ".paper-pdf-api-ocr.json")
        with file_lock(output.with_suffix(output.suffix + ".lock"), args.lock_timeout):
            key = cache_key(pdf, args)
            if not args.force and cache_valid(sidecar, output, key):
                print(json.dumps({"status": "cache-hit", "output": str(output), "sidecar": str(sidecar)}, ensure_ascii=False))
                return 0
            result = convert(pdf, output, args)
            print(json.dumps({
                "status": "converted", "output": str(output), "sidecar": str(sidecar),
                "pages": len(result["pages"]), "elapsed_seconds": result["backend"]["elapsed_seconds"],
            }, ensure_ascii=False))
            return EXIT_OK
    except QuotaExhausted as exc:
        # Exit 3 lets a router distinguish "spent allowance" from a real failure.
        print(f"paper-pdf-api-ocr: QUOTA: {exc}", file=sys.stderr)
        return EXIT_QUOTA
    except ConversionError as exc:
        print(f"paper-pdf-api-ocr: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
