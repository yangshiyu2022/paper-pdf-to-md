param(
  [string]$TaskName = "Codex PDF Literature Ingest",
  [string]$At = "08:00",
  [int]$WindowHours = 24,
  [string]$Library = "",
  [int]$Retries = 2,
  [switch]$Cloud,
  [switch]$CopySource
)
$ErrorActionPreference = "Stop"

$Ingest = Join-Path $PSScriptRoot "ingest_recent_pdfs.ps1"
if (-not (Test-Path -LiteralPath $Ingest)) { throw "Ingest wrapper not found: $Ingest" }
if ([string]::IsNullOrWhiteSpace($Library)) { $Library = $env:PAPER_LIBRARY_ROOT }
if ([string]::IsNullOrWhiteSpace($Library)) { throw "Pass -Library or set PAPER_LIBRARY_ROOT before registering the task." }
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Ingest`" -WindowHours $WindowHours -Library `"$Library`" -Retries $Retries"
if ($Cloud) { $arguments += " -Cloud" }
if ($CopySource) { $arguments += " -CopySource" }
$action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument $arguments
$triggerTime = [datetime]::ParseExact($At, "HH:mm", $null)
$firstRun = (Get-Date).Date.Add($triggerTime.TimeOfDay)
if ($firstRun -le (Get-Date)) { $firstRun = $firstRun.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $firstRun
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Scan PDFs downloaded in the last 24 hours, deduplicate, and convert likely research papers to Markdown." -Force | Out-Null
Write-Output "Registered '$TaskName' at $At every day for user $env:USERNAME. Library: $Library"
