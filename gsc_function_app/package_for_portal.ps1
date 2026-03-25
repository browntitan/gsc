param(
  [string]$OutputPath = (Join-Path $PSScriptRoot "dist\gsc_function_app_portal.zip")
)

$ErrorActionPreference = "Stop"

$distDir = Split-Path -Parent $OutputPath
if (-not (Test-Path $distDir)) {
  New-Item -ItemType Directory -Path $distDir -Force | Out-Null
}

$excludeNames = @(
  ".venv",
  "venv",
  "__pycache__",
  ".vscode",
  "dist"
)

$itemsToArchive = Get-ChildItem -Force $PSScriptRoot |
  Where-Object { $excludeNames -notcontains $_.Name }

if ($itemsToArchive.Count -eq 0) {
  throw "No files were found to archive from $PSScriptRoot."
}

Compress-Archive -Path $itemsToArchive.FullName -DestinationPath $OutputPath -Force

Write-Host "Created portal deployment package:"
Write-Host "  $OutputPath"
Write-Host ""
Write-Host "The ZIP root now contains function_app.py, host.json, and requirements.txt directly."
