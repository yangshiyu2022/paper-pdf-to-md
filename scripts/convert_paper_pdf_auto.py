#!/usr/bin/env python
"""Route one PDF to the MinerU cloud API first, falling back to local conversion.

Exit 3 from the API backend means the allowance is spent. Any other API failure
also falls back, because the local backend is the safety net.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

EXIT_QUOTA = 3

# Force UTF-8 I/O regardless of how we were launched. PYTHONIOENCODING is not
# inherited through PowerShell Start-Process, and the locale is GBK on zh-CN
# Windows, so CJK paths would raise UnicodeEncodeError on print.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
API_SCRIPT = HERE / "convert_paper_pdf_api.py"
LOCAL_SCRIPT_CANDIDATES = (
    HERE / "convert_paper_pdf.py",
    HERE.parent.parent / "paper-pdf-local-ocr" / "scripts" / "convert_paper_pdf.py",
    Path.home() / ".codex" / "skills" / "paper-pdf-local-ocr" / "scripts" / "convert_paper_pdf.py",
    Path.home() / ".claude" / "skills" / "paper-pdf-local-ocr" / "scripts" / "convert_paper_pdf.py",
)

QUOTA_MARKERS = (
    "quota", "配额", "额度", "rate limit", "too many requests",
    "exceeded", "limit exceeded", "insufficient",
)


def local_script() -> Path:
    for candidate in LOCAL_SCRIPT_CANDIDATES:
        if candidate.exists():
            return candidate
    raise SystemExit("paper-pdf-auto: could not locate the local converter (paper-pdf-local-ocr).")


def token_available() -> bool:
    """Return whether a cloud token is configured without reading its value."""
    if os.environ.get("MINERU_API_TOKEN", "").strip():
        return True
    return any(path.exists() for path in (
        Path.home() / ".codex" / "secrets" / "mineru-api.json",
        Path.home() / ".claude" / "secrets" / "mineru-api.json",
    ))


def looks_like_quota(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in QUOTA_MARKERS)


def run(script: Path, argv: list[str]) -> tuple[int, str, str]:
    """Run a converter, forcing UTF-8 on both ends of the pipe.

    -I ignores the environment, so PYTHONIOENCODING must be set in env= rather
    than inherited, or the child writes GBK and this decode produces U+FFFD.
    """
    command = [sys.executable, "-I", str(script), *argv]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", check=False, env=env)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def emit(stream: str, prefix: str) -> None:
    for line in (stream or "").splitlines():
        if line.strip():
            print(f"{prefix} {line}", file=sys.stderr)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="API-first PDF to Markdown with local fallback.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--ocr", choices=["off", "auto", "always"], default="auto")
    parser.add_argument("--formula", choices=["off", "auto", "always"], default="auto")
    parser.add_argument("--language", default="en")
    parser.add_argument("--review-dir")
    parser.add_argument("--force", action="store_true", help="Bypass a cache hit in the chosen backend.")
    parser.add_argument("--force-local", action="store_true", help="Skip the API entirely; never uploads.")
    parser.add_argument("--force-api", action="store_true", help="Fail instead of falling back to local.")
    parser.add_argument("--api-timeout", type=float, default=1800.0)
    parser.add_argument("--mineru-timeout", type=float, default=3600.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.force_local and args.force_api:
        print("paper-pdf-auto: --force-local and --force-api are mutually exclusive.", file=sys.stderr)
        return 2

    shared = [str(args.pdf), "-o", str(args.output), "--ocr", args.ocr,
              "--formula", args.formula, "--language", args.language]
    if args.review_dir:
        shared += ["--review-dir", args.review_dir]
    if args.force:
        shared.append("--force")

    started = time.time()
    if not args.force_local and not token_available():
        if args.force_api:
            print(
                "paper-pdf-auto: no MinerU API token configured. Register at "
                "https://mineru.net/ and set MINERU_API_TOKEN; refusing cloud mode.",
                file=sys.stderr,
            )
            return 2
        reason = "no-api-token"
        print(
            "paper-pdf-auto: no MinerU API key found. Register at https://mineru.net/ "
            "to obtain one; using local MinerU compute only.",
            file=sys.stderr,
        )
    elif not args.force_local:
        code, out, err = run(API_SCRIPT, shared + ["--api-timeout", str(args.api_timeout)])
        emit(err, "paper-pdf-auto[api]")
        if code == 0:
            payload = {"backend": "api", "fallback": False}
            try:
                payload.update(json.loads(out.strip().splitlines()[-1]))
            except Exception:
                payload["raw"] = out.strip()[-400:]
            payload["router_seconds"] = round(time.time() - started, 1)
            print(json.dumps(payload, ensure_ascii=False))
            return 0

        quota = code == EXIT_QUOTA or looks_like_quota(err) or looks_like_quota(out)
        reason = "quota-exhausted" if quota else f"api-failed-exit-{code}"
        if args.force_api:
            print(f"paper-pdf-auto: API failed ({reason}) and local fallback is disabled.", file=sys.stderr)
            return code or 2
        print(f"paper-pdf-auto: falling back to local backend ({reason}).", file=sys.stderr)
    else:
        reason = "forced-local"

    code, out, err = run(local_script(), shared + ["--mineru-timeout", str(args.mineru_timeout)])
    emit(err, "paper-pdf-auto[local]")
    if code != 0:
        print(f"paper-pdf-auto: local backend also failed (exit {code}).", file=sys.stderr)
        return code or 2
    payload = {"backend": "local", "fallback": not args.force_local, "fallback_reason": reason}
    try:
        payload.update(json.loads(out.strip().splitlines()[-1]))
    except Exception:
        payload["raw"] = out.strip()[-400:]
    payload["router_seconds"] = round(time.time() - started, 1)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
