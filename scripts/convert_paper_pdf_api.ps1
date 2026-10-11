$ErrorActionPreference = "Stop"
$Python = $env:MINERU_PYTHON
if ([string]::IsNullOrWhiteSpace($Python)) {
  $Runtime = $env:MINERU_RUNTIME
  if (-not [string]::IsNullOrWhiteSpace($Runtime)) {
    $Python = Join-Path $Runtime "python.exe"
    if (-not (Test-Path -LiteralPath $Python)) { $Python = Join-Path $Runtime "Scripts\python.exe" }
  }
}
if ([string]::IsNullOrWhiteSpace($Python) -or -not (Test-Path -LiteralPath $Python)) {
  $command = Get-Command python -ErrorAction SilentlyContinue
  if ($command) { $Python = $command.Source }
}
$Script = Join-Path $PSScriptRoot "convert_paper_pdf_api.py"

if (-not (Test-Path -LiteralPath $Python)) {
  throw "Python not found. Set MINERU_PYTHON or MINERU_RUNTIME to a runtime with MinerU installed."
}
if (-not (Test-Path -LiteralPath $Script)) {
  throw "convert_paper_pdf_api.py not found: $Script"
}

$env:PYTHONIOENCODING = "utf-8"
& $Python -I $Script @args
exit $LASTEXITCODE
