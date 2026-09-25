$ErrorActionPreference = "Stop"
$worktree = "C:\Users\csfri\AppData\Local\Temp\opencode\fca-rust\ws-lake"
$dataDir = "C:\Users\csfri\AppData\Local\Temp\lake-onesession"
$trace = "C:\Users\csfri\AppData\Local\Temp\lake-onesession-stderr.log"
$port = 8130
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
Get-Process lake -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Remove-Item -Recurse -Force $dataDir -ErrorAction SilentlyContinue
Remove-Item -Force $trace -ErrorAction SilentlyContinue

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
    $t0 = Get-Date
    $payload = @{
        asset_class = "equity"
        data_type   = "trade"
        symbol      = "SPY"
        start_date  = "2026-09-21"
        end_date    = "2026-09-21"
    } | ConvertTo-Json
    $ensure = Invoke-RestMethod -Method Post -Uri "$base/v1/catalog/ensure" -Headers $headers -ContentType "application/json" -Body $payload -TimeoutSec 120
    $pollDeadline = $t0.AddMinutes(5)
    while ((Get-Date) -lt $pollDeadline) {
        Start-Sleep -Milliseconds 500
        $coverage = Invoke-RestMethod -Uri "$base/v1/catalog/coverage?asset_class=equity&data_type=trade&symbol=SPY&start_date=2026-09-21&end_date=2026-09-21" -Headers $headers -TimeoutSec 30
        if ($coverage.entries[0].status -in @("complete", "empty")) { break }
    }
    $elapsed = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    Write-Output ("rows=" + $coverage.entries[0].row_count + " elapsed_s=" + $elapsed)

    $gets = Get-Content $trace | Where-Object { $_ -like 'rca get*' }
    $bytes = ($gets | ForEach-Object { if ($_ -match 'decoded=(\d+)') { [long]$Matches[1] } } | Measure-Object -Sum).Sum
    $lane = Get-Content $trace | Where-Object { $_ -like 'rca lane*' }
    $bounds = $lane | ForEach-Object { if ($_ -match 'bound=(\d+)') { [int]$Matches[1] } }
    Write-Output ("requests=" + $gets.Count + " decoded_MB=" + [math]::Round($bytes/1MB,1) + " lane_samples=" + $lane.Count + " bound_max=" + ($bounds | Measure-Object -Maximum).Maximum)
}
finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
