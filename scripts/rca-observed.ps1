$ErrorActionPreference = "Stop"
$worktree = "C:\Users\csfri\AppData\Local\Temp\opencode\fca-rust\ws-lake"
$dataDir = "C:\Users\csfri\AppData\Local\Temp\lake-rca-two"
$trace = "C:\Users\csfri\AppData\Local\Temp\lake-rca2-stderr.log"
$pollLog = "C:\Users\csfri\AppData\Local\Temp\lake-rca2-polls.log"
$port = 8127
$base = "http://127.0.0.1:$port"

Get-Content "C:\Users\csfri\source\repos\FCA\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
$env:LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY = "64"
$env:LAKE_PROVIDER__REQUEST__REQUESTS_PER_MINUTE = "8000"
$env:LAKE_RCA_LOG = "1"
$env:LAKE_LOCAL_DATA_DIR = $dataDir
Remove-Item -Recurse -Force $dataDir -ErrorAction SilentlyContinue
Remove-Item -Force $trace, $pollLog -ErrorAction SilentlyContinue

$binary = "$worktree\src\lake\target\release\lake.exe"
$server = Start-Process -FilePath $binary -ArgumentList "local", "api", "--port", "$port" -PassThru -NoNewWindow -RedirectStandardError $trace
try {
    $ready = $false
    foreach ($attempt in 1..50) {
        try {
            $health = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 2
            if ($health.status -eq "ok") { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $ready) { throw "local api did not become healthy" }

    $headers = @{ Authorization = "Bearer $env:LAKE_API__BEARER_TOKEN" }
    $t0 = Get-Date
    $payload = @{
        asset_class = "equity"
        data_type   = "quote"
        symbol      = "SPY"
        start_date  = "2026-09-21"
        end_date    = "2026-09-21"
    } | ConvertTo-Json
    $ensure = Invoke-RestMethod -Method Post -Uri "$base/v1/catalog/ensure" -Headers $headers -ContentType "application/json" -Body $payload -TimeoutSec 60
    $jobId = $ensure.job_id
    "ensure job_id=$jobId" | Set-Content $pollLog

    $pollDeadline = $t0.AddSeconds(90)
    while ((Get-Date) -lt $pollDeadline) {
        Start-Sleep -Milliseconds 500
        $now = (Get-Date).ToString("HH:mm:ss.fff")
        $coverage = Invoke-RestMethod -Uri "$base/v1/catalog/coverage?asset_class=equity&data_type=quote&symbol=SPY&start_date=2026-09-21&end_date=2026-09-21" -Headers $headers -TimeoutSec 20
        $status = $coverage.entries[0].status
        $rows = $coverage.entries[0].row_count
        $job = Invoke-RestMethod -Uri "$base/v1/jobs/$jobId" -Headers $headers -TimeoutSec 20
        $state = $job.state
        "$now status=$status rows=$rows job=$state" | Add-Content $pollLog
        if ($status -in @("complete", "empty")) { break }
    }
    Get-Content $pollLog | Select-Object -Last 8
}
finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
}
