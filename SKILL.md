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

## Review requirements

MinerU's Markdown and UniMERNet formula LaTeX are model output. Read the sidecar and `text_review.json`, inspect flagged crops, and verify formulas or tables used in downstream claims against the source PDF. Do not promise perfect OCR or formula recovery.

Read [references/backend-and-review.md](references/backend-and-review.md) when changing backend behavior, provisioning models, or auditing cache integrity.
