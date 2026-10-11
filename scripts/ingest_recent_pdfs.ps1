param(
  [int]$WindowHours = 24,
  [string]$Downloads = "",
  [string]$Library = "",
  [switch]$Cloud,
  [switch]$DryRun,
  [switch]$CopySource
)
$ErrorActionPreference = "Stop"

$Script = Join-Path $PSScriptRoot "ingest_recent_pdfs.py"
$Python = $env:MINERU_PYTHON
if ([string]::IsNullOrWhiteSpace($Python)) {
  $Runtime = $env:MINERU_RUNTIME
  if ([string]::IsNullOrWhiteSpace($Runtime)) {
    $Runtime = Join-Path $env:USERPROFILE ".codex\tools\paper-pdf-local-ocr\runtime"
  }
  $Python = Join-Path $Runtime "python.exe"
  if (-not (Test-Path -LiteralPath $Python)) { $Python = Join-Path $Runtime "Scripts\python.exe" }
}
if (-not (Test-Path -LiteralPath $Python)) {
  $command = Get-Command python -ErrorAction SilentlyContinue
  if ($command) { $Python = $command.Source }
}
if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found. Set MINERU_PYTHON or MINERU_RUNTIME." }
if (-not (Test-Path -LiteralPath $Script)) { throw "Ingest script not found: $Script" }
if ([string]::IsNullOrWhiteSpace($Library)) { $Library = $env:PAPER_LIBRARY_ROOT }
if ([string]::IsNullOrWhiteSpace($Library)) { throw "Pass -Library or set PAPER_LIBRARY_ROOT." }

$argsList = @($Script, "--window-hours", $WindowHours, "--library", $Library)
if ($Downloads) { $argsList += @("--downloads", $Downloads) }
if ($Cloud) { $argsList += "--cloud" }
if ($DryRun) { $argsList += "--dry-run" }
if ($CopySource) { $argsList += "--copy-source" }
$env:PYTHONIOENCODING = "utf-8"
& $Python -I @argsList
exit $LASTEXITCODE
