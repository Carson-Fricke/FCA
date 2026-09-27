$ErrorActionPreference = "Stop"
$worktree = "C:\Users\csfri\AppData\Local\Temp\opencode\fca-rust\ws-lake"
$dataDir = "C:\Users\csfri\AppData\Local\Temp\lake-ensure-test"
$port = 8124
$base = "http://127.0.0.1:$port"

# Load credentials from the repository .env into this process only; never print them.
Get-Content "C:\Users\csfri\source\repos\FCA\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
if (-not $env:LAKE_PROVIDER__API_KEY -or -not $env:LAKE_PROVIDER__API_SECRET) {
    throw "provider credentials are not configured"
}
$env:LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY = "64"
$env:LAKE_PROVIDER__REQUEST__REQUESTS_PER_MINUTE = "8000"
$env:LAKE_LOCAL_DATA_DIR = $dataDir
Remove-Item -Recurse -Force $dataDir -ErrorAction SilentlyContinue

$binary = "$worktree\src\lake\target\release\lake.exe"
$server = Start-Process -FilePath $binary -ArgumentList "local", "api", "--port", "$port" -PassThru -NoNewWindow
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
    $results = @()
    foreach ($dataType in @("trade", "quote")) {
        $t0 = Get-Date
        $payload = @{
            asset_class = "equity"
            data_type   = $dataType
            symbol      = "SPY"
            start_date  = "2026-07-01"
            end_date    = "2026-08-31"
        } | ConvertTo-Json
        $ensure = Invoke-RestMethod -Method Post -Uri "$base/v1/catalog/ensure" -Headers $headers -ContentType "application/json" -Body $payload -TimeoutSec 60
        $acceptedViaJob = $false
        if ($ensure.PSObject.Properties.Name -contains "job_id") { $acceptedViaJob = $true }

        $terminal = 0
        $total = 0
        $rows = 0
        $bytes = 0
        $pollDeadline = $t0.AddMinutes(25)
        do {
            Start-Sleep -Milliseconds 500
            $coverage = Invoke-RestMethod -Uri "$base/v1/catalog/coverage?asset_class=equity&data_type=$dataType&symbol=SPY&start_date=2026-07-01&end_date=2026-08-31" -Headers $headers -TimeoutSec 30
            $terminal = ($coverage.entries | Where-Object { $_.status -in @("complete", "empty") }).Count
            $total = $coverage.entries.Count
            $rows = ($coverage.entries | Measure-Object -Property row_count -Sum).Sum
            $bytes = ($coverage.entries | Measure-Object -Property byte_size -Sum).Sum
        } until (($terminal -eq $total -and $total -gt 0) -or ((Get-Date) -gt $pollDeadline))
        $elapsed = ((Get-Date) - $t0).TotalSeconds
        $results += [pscustomobject]@{
            data_type = $dataType
            accepted_via_job = $acceptedViaJob
            sessions = $total
            terminal = $terminal
            rows = $rows
            megabytes = [math]::Round($bytes / 1MB, 1)
            seconds = [math]::Round($elapsed, 1)
        }
        Write-Output ($results[-1] | ConvertTo-Json -Compress)
    }
    Write-Output "SUMMARY"
    $results | ConvertTo-Json -Compress
}
finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
