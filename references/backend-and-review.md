# Backend and Review Notes

## Backend policy

- Use only local files and local model inference. Do not upload PDFs, page renders, crops, or extracted text to remote OCR or vision APIs.
- MinerU is the only Markdown-producing backend. Local OCR and formula models are review-only evidence generators.
- Treat model setup as explicit provisioning. Conversion commands run offline and fail when required local dependencies or model weights are missing.

## Text extraction

- Prefer native PDF text for born-digital papers.
- Treat pages with very low native text as scan-like and use local OCR only when --ocr auto or --ocr always requires it.
- Use PyMuPDF block geometry and fonts to preserve page anchors and identify formula candidates.

## Formula extraction

- Formula LaTeX comes from MinerU's local UniMERNet MFR model (models/MFR/unimernet_hf_small_2503),
  enabled by passing `--formula true` to the MinerU CLI. `--formula off` disables it.
- Detect formula candidates from math fonts, centered symbolic blocks, equation numbers, and symbolic
  density. This detection now only feeds `--inspect-only` reports and the sidecar `formula_candidates`
  count; it no longer drives a separate recognition pass.
- UniMERNet does not provide calibrated confidence. Do not report confidence percentages.
- Treat model-recognized formulas as requiring human review against the source PDF whenever a
  downstream claim depends on them.

## Review artifacts

Each conversion writes:

- Markdown output produced by MinerU, with its image assets retained under the review directory.
- output.paper-pdf-local-ocr.json sidecar with PDF hash, output hash, options, page modes, MinerU
  metadata, the ASCII stem used for staging, and per-page formula-candidate counts.
- text_review.json under the review directory, listing scan-like pages and ambiguous text blocks.
- text_crops and page_images PNG files for visual checking of flagged items.

## Cache and integrity

- Cache hits are valid only when the sidecar cache key matches the current PDF hash/options and the Markdown output hash still matches the sidecar.
- Use --force to refresh a conversion.
- Do not trust a stale sidecar if the Markdown was edited by hand.

## Known limits

- Formula OCR can produce plausible but wrong LaTeX; always inspect crops for equations used in downstream analysis.
- Native PDF text for math fonts can be useful evidence but is often not valid LaTeX.
- RapidOCR deployment and model paths must be checked with doctor.py before relying on scanned-page OCR.
