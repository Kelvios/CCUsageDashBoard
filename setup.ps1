# Phase 1 local setup (Windows PowerShell). Run from the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
$claude = Join-Path $HOME ".claude"
$settings = Join-Path $claude "settings.json"
New-Item -ItemType Directory -Force $claude | Out-Null

# 1. Stop log pruning: cleanupPeriodDays = 365 (merge, keep other keys)
$obj = if (Test-Path $settings) { Get-Content $settings -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if ($null -eq $obj) { $obj = [pscustomobject]@{} }
if (Test-Path $settings) { Copy-Item $settings "$settings.bak" -Force }
$obj | Add-Member -NotePropertyName cleanupPeriodDays -NotePropertyValue 365 -Force
$obj | ConvertTo-Json -Depth 32 | Set-Content $settings -Encoding UTF8
Write-Host "cleanupPeriodDays=365 set in $settings (backup: settings.json.bak)"

# How far back do the logs go?
$files = Get-ChildItem (Join-Path $claude "projects") -Recurse -Filter *.jsonl -ErrorAction SilentlyContinue
if ($files) {
  $oldest = ($files | Sort-Object LastWriteTime | Select-Object -First 1).LastWriteTime
  Write-Host ("{0} log files; oldest last-modified {1:yyyy-MM-dd} ({2} days ago)" -f $files.Count, $oldest, [int]((Get-Date) - $oldest).TotalDays)
} else { Write-Host "No ~/.claude/projects/**/*.jsonl found" }

# 2. ccusage
npm i -g ccusage
ccusage claude daily --breakdown --timezone Australia/Brisbane

# 3. first collection (backfills everything that survives)
python collect.py
