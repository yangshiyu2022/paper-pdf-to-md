---
name: paper-pdf-to-md
description: Convert local academic PDFs to reviewable Markdown with MinerU, choosing the MinerU cloud API only when a user-configured key and upload permission are present, otherwise using local MinerU compute.
---

# PDF to Markdown with MinerU

Use this skill when a user wants a local PDF converted to Markdown while preserving formulas, tables, images, and review evidence.

## Backend selection

- The default entry point is `scripts/convert_paper_pdf_auto.ps1`.
- If `MINERU_API_TOKEN` is set, or a secrets file exists at `%USERPROFILE%\.codex\secrets\mineru-api.json`, the router can use the MinerU cloud API. Cloud conversion uploads the PDF to `https://mineru.net`; use it only for a non-confidential PDF after the user has accepted that upload.
- If no key is configured, tell the user to register at [mineru.net](https://mineru.net/) and create an API key. Continue with local MinerU compute only; never attempt an unauthenticated upload.
- Use `--force-local` for confidential, unpublished, or otherwise local-only PDFs. Use `--force-api` only when the user explicitly wants cloud conversion and has configured a key.
- When the cloud API is unavailable, the token is rejected, or the quota is exhausted, the router falls back to local MinerU unless `--force-api` was requested.

## Commands

```powershell
# Automatic route: cloud only when a key is configured; otherwise local
scripts\convert_paper_pdf_auto.ps1 "paper.pdf" -o "paper.md" --language en

# Local-only conversion; no network OCR or API upload
scripts\convert_paper_pdf.ps1 "paper.pdf" -o "paper.md" --ocr auto --formula auto

# Check local runtime and model availability
scripts\doctor.py
```

Set `MINERU_PYTHON` to the Python executable in the environment where MinerU is installed, or set `MINERU_RUNTIME` to that environment's root. Provision the local pipeline models before the first local conversion. The local backend writes a Markdown file, a JSON sidecar, page/text review evidence, and retained image assets.

## Daily literature intake

To install a Windows task that runs daily at 08:00 and checks only PDFs created or modified in the previous 24 hours:

```powershell
$env:PAPER_LIBRARY_ROOT = "E:\Agentwork\codex\Thesispaper\paper\参考文献\参考文献（全部）"
scripts\register_daily_ingest_task.ps1 -Library $env:PAPER_LIBRARY_ROOT
```

Run `scripts\ingest_recent_pdfs.ps1 -Library $env:PAPER_LIBRARY_ROOT -DryRun` first to preview candidates. The task uses local MinerU, retries each failed conversion twice by default, moves a unique PDF into `09_待规范命名` only after Markdown conversion succeeds, and writes Markdown beneath `09_待规范命名MD`. Exact-hash and DOI duplicates are skipped; title or filename matches are flagged as suspected duplicates. Suspected duplicates and files that cannot confidently be classified remain in Downloads and are recorded in the report. It does not delete duplicates or upload documents to the cloud.

## Review requirements

MinerU's Markdown and UniMERNet formula LaTeX are model output. Read the sidecar and `text_review.json`, inspect flagged crops, and verify formulas or tables used in downstream claims against the source PDF. Do not promise perfect OCR or formula recovery.

Read [references/backend-and-review.md](references/backend-and-review.md) when changing backend behavior, provisioning models, or auditing cache integrity.
