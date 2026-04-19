param(
  [int]$IntervalSeconds = 30,
  [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

function HasChanges {
  $status = git status --porcelain
  return -not [string]::IsNullOrWhiteSpace(($status -join "`n"))
}

Write-Host "[auto] Starting auto commit/push loop..."
Write-Host "[auto] Interval: $IntervalSeconds seconds, Branch: $Branch"
Write-Host "[auto] Press Ctrl + C to stop."

while ($true) {
  Start-Sleep -Seconds $IntervalSeconds

  if (-not (HasChanges)) {
    continue
  }

  # local SQLite file is runtime data; keep it out of auto commits
  git add -A
  git reset -- "hanam.db" | Out-Null

  if (-not (HasChanges)) {
    continue
  }

  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $msg = "auto: sync changes ($timestamp)"

  try {
    git commit -m $msg | Out-Null
    git push origin $Branch | Out-Null
    Write-Host "[auto] Pushed: $msg"
  } catch {
    Write-Host "[auto] Commit/push failed. Retrying next cycle..."
  }
}
