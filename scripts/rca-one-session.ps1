$ErrorActionPreference = "Stop"
$worktree = "C:\Users\csfri\AppData\Local\Temp\opencode\fca-rust\ws-lake"
$dataDir = "C:\Users\csfri\AppData\Local\Temp\lake-rca-one"
$trace = "C:\Users\csfri\AppData\Local\Temp\lake-rca-stderr.log"
$port = 8125
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
Remove-Item -Force $trace -ErrorAction SilentlyContinue

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
    Write-Output ("ensure accepted as job: " + ($ensure.PSObject.Properties.Name -contains "job_id"))

    $terminal = 0
    $total = 0
    $rows = 0
    $pollDeadline = $t0.AddMinutes(10)
    do {
        Start-Sleep -Milliseconds 300
        $coverage = Invoke-RestMethod -Uri "$base/v1/catalog/coverage?asset_class=equity&data_type=quote&symbol=SPY&start_date=2026-09-21&end_date=2026-09-21" -Headers $headers -TimeoutSec 30
        $terminal = ($coverage.entries | Where-Object { $_.status -in @("complete", "empty") }).Count
        $total = $coverage.entries.Count
        $rows = ($coverage.entries | Measure-Object -Property row_count -Sum).Sum
    } until (($terminal -eq $total -and $total -gt 0) -or ((Get-Date) -gt $pollDeadline))
    $elapsed = ((Get-Date) - $t0).TotalSeconds
    Write-Output ("terminal=$terminal/$total rows=$rows elapsed_s=" + [math]::Round($elapsed, 1))
}
finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
}
Write-Output "TRACE"
Get-Content $trace -ErrorAction SilentlyContinue | Select-Object -First 12
Write-Output "..."
Get-Content $trace -ErrorAction SilentlyContinue | Select-Object -Last 6
