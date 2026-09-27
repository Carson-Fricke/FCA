$ErrorActionPreference = "Stop"
$worktree = "C:\Users\csfri\AppData\Local\Temp\opencode\fca-rust\ws-lake"
$dataDir = "C:\Users\csfri\AppData\Local\Temp\lake-week"
$trace = "C:\Users\csfri\AppData\Local\Temp\lake-week-stderr.log"
$pollLog = "C:\Users\csfri\AppData\Local\Temp\lake-week-polls.log"
$resultPath = "C:\Users\csfri\AppData\Local\Temp\lake-week-result.json"
$port = 8129
$base = "http://127.0.0.1:$port"

Get-Content "C:\Users\csfri\source\repos\FCA\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
$env:LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY = "512"
$env:LAKE_PROVIDER__REQUEST__REQUESTS_PER_MINUTE = "8000"
$env:LAKE_RCA_LOG = "1"
$env:LAKE_LOCAL_DATA_DIR = $dataDir
Remove-Item -Recurse -Force $dataDir -ErrorAction SilentlyContinue
Remove-Item -Force $trace, $pollLog, $resultPath -ErrorAction SilentlyContinue

$binary = "$worktree\src\lake\target\release\lake.exe"
$server = Start-Process -FilePath $binary -ArgumentList "local", "api", "--port", "$port" -PassThru -NoNewWindow -RedirectStandardError $trace
try {
    $ready = $false
    foreach ($attempt in 1..100) {
        try {
            $health = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 2
            if ($health.status -eq "ok") { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $ready) { throw "local api did not become healthy" }

    $headers = @{ Authorization = "Bearer $env:LAKE_API__BEARER_TOKEN" }
    $results = @()
    foreach ($dataType in @("trade", "quote")) {
        $t0 = Get-Date
        $payload = @{
            asset_class = "equity"
            data_type   = $dataType
            symbol      = "SPY"
            start_date  = "2026-09-14"
            end_date    = "2026-09-18"
        } | ConvertTo-Json
        $ensure = Invoke-RestMethod -Method Post -Uri "$base/v1/catalog/ensure" -Headers $headers -ContentType "application/json" -Body $payload -TimeoutSec 120
        $jobId = $ensure.job_id
        "$($t0.ToString('HH:mm:ss.fff')) ensure $dataType job=$jobId" | Add-Content $pollLog

        $pollDeadline = $t0.AddMinutes(6)
        $terminal = 0
        $total = 0
        $rows = 0
        $bytes = 0
        while ((Get-Date) -lt $pollDeadline) {
            Start-Sleep -Milliseconds 1000
            $coverage = Invoke-RestMethod -Uri "$base/v1/catalog/coverage?asset_class=equity&data_type=$dataType&symbol=SPY&start_date=2026-09-14&end_date=2026-09-18" -Headers $headers -TimeoutSec 30
            $terminal = ($coverage.entries | Where-Object { $_.status -in @("complete", "empty") }).Count
            $total = $coverage.entries.Count
            $rows = ($coverage.entries | Measure-Object -Property row_count -Sum).Sum
            $bytes = ($coverage.entries | Measure-Object -Property byte_size -Sum).Sum
            $now = (Get-Date).ToString("HH:mm:ss.fff")
            "$now $dataType terminal=$terminal/$total rows=$rows MB=$([math]::Round($bytes/1MB,1))" | Add-Content $pollLog
            if ($total -gt 0 -and $terminal -eq $total) { break }
        }
        $elapsed = ((Get-Date) - $t0).TotalSeconds
        $results += [pscustomobject]@{
            data_type = $dataType
            sessions = $total
            terminal = $terminal
            rows = $rows
            megabytes = [math]::Round($bytes / 1MB, 1)
            seconds = [math]::Round($elapsed, 1)
        }
    }
    $results | ConvertTo-Json -Compress | Set-Content $resultPath
}
finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
