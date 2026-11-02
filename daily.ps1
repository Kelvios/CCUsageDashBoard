# Daily usage collection (collect only). Task Scheduler runs it at 06:00.
# It queues new/changed rows in data\pending and records the outcome in
# data\last-collect.json. The upload is done by the Claude session on this PC,
# woken by a scheduled task 30 minutes later, and only after a successful
# collection (see README "Daily collection and push").
# Only token counts, costs, model names and project folder names are queued.
$ErrorActionPreference = "Continue"
$Repo = "D:\repositories\CCUsageDashboard"
Set-Location $Repo

$Pending = Join-Path $Repo "data\pending"
$Log = Join-Path $Repo "data\daily.log"
$Status = Join-Path $Repo "data\last-collect.json"
New-Item -ItemType Directory -Force $Pending | Out-Null

function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content -Path $Log -Encoding utf8 }

$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$file = Join-Path $Pending "$stamp.json"
# Conductor plan-limit readings (row kind "usage"), if that repo is checked out here.
$UsageLog = "D:\Repositories\Conductor\logs\usage.md"
$extra = @(); if (Test-Path $UsageLog) { $extra = @("--usage-log", $UsageLog) } else { Log "no Conductor usage log at $UsageLog (skipped)" }

$out = & python collect.py --changed-rows $file @extra 2>&1
$code = $LASTEXITCODE
$out | ForEach-Object { Log "collect: $_" }

$ok = ($code -eq 0) -and (Test-Path $file)
$rows = 0
if ($ok) { try { $rows = @(Get-Content $file -Raw | ConvertFrom-Json).Count } catch { $ok = $false } }
if ($ok) { Log "COLLECT OK $stamp.json ($rows rows)" } else { Log "COLLECT FAILED (exit $code)" }

[ordered]@{
  at      = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
  ok      = $ok
  file    = if ($ok) { "$stamp.json" } else { $null }
  rows    = $rows
  exit    = $code
} | ConvertTo-Json | Set-Content -Path $Status -Encoding utf8

if ($ok) { exit 0 } else { exit 1 }
