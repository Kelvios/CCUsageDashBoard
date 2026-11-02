# Daily usage push: collect new/changed rows locally, then have headless
# Claude Code upsert them into the dashboard artifact's `usage` collection.
# Only token counts, costs, model names and project folder names are sent.
# Scheduled by Task Scheduler (see README); safe to run by hand.
$ErrorActionPreference = "Continue"
$Repo = "D:\repositories\CCUsageDashboard"
$ArtifactUrl = "https://claude.ai/artifact/CUQXm9pjEjWAqpcByZzGQf"
Set-Location $Repo

$Pending = Join-Path $Repo "data\pending"
$Log = Join-Path $Repo "data\daily.log"
New-Item -ItemType Directory -Force $Pending | Out-Null

function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content -Path $Log -Encoding utf8 }

# 1. Collect. Always write a pending file; a failed push leaves it for the next run.
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
# Conductor plan-limit readings (row kind "usage"), if that repo is checked out here.
$UsageLog = "D:\Repositories\Conductor\logs\usage.md"
$extra = @(); if (Test-Path $UsageLog) { $extra = @("--usage-log", $UsageLog) } else { Log "no Conductor usage log at $UsageLog (skipped)" }
$out = & python collect.py --changed-rows (Join-Path $Pending "$stamp.json") @extra 2>&1
$out | ForEach-Object { Log "collect: $_" }
if ($LASTEXITCODE -ne 0) { Log "collect.py failed (exit $LASTEXITCODE); pending files from earlier runs are still tried"; }

$files = Get-ChildItem $Pending -Filter *.json -ErrorAction SilentlyContinue | Sort-Object Name
if (-not $files) { Log "nothing pending"; exit 0 }
$names = ($files | ForEach-Object { $_.Name }) -join ", "
Log "pending: $names"

# 2. Headless Claude upserts the rows. It may only load ArtifactData and read
#    files under data\pending; the script deletes files itself afterwards.
$prompt = @"
Upsert usage rows into an artifact database. Artifact URL: $ArtifactUrl
Files (in data/pending/): $names
Each file is a JSON array of row objects. Every row has a string field "key".
First load the ArtifactData tool schema with ToolSearch (select:ArtifactData). Then, for each file:
1. Read the file.
2. Write every row to the collection "usage" with ArtifactData action "batch", op "set", doc_id = the row's "key", data = the whole row object, up to 50 rows per batch. Rows that already exist need if_version: first read them with action "get" (or use a "query" on the collection) and pass the current version as if_version; rows that do not exist are created without it.
3. Only when every batch for that file succeeded, print exactly one line: DONE <filename>
If any write for a file fails, print FAILED <filename> and the error, and continue with the next file. Do not do anything else: no other tools, no other collections, no changes to the artifact page. Never include anything but the numbers and names already in the rows.
"@

$tools = @("ArtifactData", "ToolSearch", "Read(data/pending/**)")
$result = & claude -p $prompt --model sonnet --allowedTools ($tools -join ",") 2>&1
$result | ForEach-Object { Log "claude: $_" }
if ($LASTEXITCODE -ne 0) { Log "claude exited $LASTEXITCODE" }

# 3. Delete only the files Claude reported as fully written.
$done = @($result | ForEach-Object { "$_" } | Where-Object { $_ -match '^DONE\s+(\S+\.json)\s*$' } | ForEach-Object { $Matches[1] })
foreach ($n in $done) {
    $p = Join-Path $Pending $n
    if ((Test-Path $p) -and ($files.Name -contains $n)) { Remove-Item $p; Log "pushed and removed $n" }
}
$left = @(Get-ChildItem $Pending -Filter *.json -ErrorAction SilentlyContinue)
if ($left.Count) { Log "$($left.Count) file(s) left for the next run" } else { Log "all pushed" }
