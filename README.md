# paper-pdf-to-md

Codex skill for converting local academic PDFs to Markdown with [MinerU](https://github.com/opendatalab/MinerU).

The automatic router uses the MinerU cloud API only when a key is configured and cloud upload is appropriate. If no key is present, it prints a registration prompt for [https://mineru.net/](https://mineru.net/) and runs the local MinerU pipeline only. A key is never stored in this repository.

## Install

Copy this directory to your Codex skills directory, for example:

```powershell
Copy-Item -Recurse .\paper-pdf-to-md "$HOME\.codex\skills\paper-pdf-to-md"
```

Install MinerU and its local model dependencies in a dedicated Python environment. Set `MINERU_PYTHON` to that environment's `python.exe`, or set `MINERU_RUNTIME` to its root. Run `scripts\doctor.py` before converting. Local model downloads are intentionally not included in this repository.

## API key (optional)

Register at [mineru.net](https://mineru.net/), create an API key, and set it for the current shell:

```powershell
$env:MINERU_API_TOKEN = "sk-..."
```

Alternatively create `%USERPROFILE%\.codex\secrets\mineru-api.json` (keep it outside Git):

```json
{"token":"sk-...","expires_on":"YYYY-MM-DD"}
```

Cloud conversion uploads the PDF. Use `scripts\convert_paper_pdf.ps1` when the PDF must remain local.
