$ErrorActionPreference = "Stop"
$worktree = "C:\Users\csfri\AppData\Local\Temp\opencode\fca-rust\ws-lake"
$dataDir = "C:\Users\csfri\AppData\Local\Temp\lake-rca-one"
$port = 8126
$base = "http://127.0.0.1:$port"

Get-Content "C:\Users\csfri\source\repos\FCA\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
$env:LAKE_LOCAL_DATA_DIR = $dataDir

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
    $coverage = Invoke-RestMethod -Uri "$base/v1/catalog/coverage?asset_class=equity&data_type=quote&symbol=SPY&start_date=2026-09-21&end_date=2026-09-21" -Headers $headers -TimeoutSec 10
    Write-Output "COVERAGE RESPONSE:"
    $coverage | ConvertTo-Json -Depth 5
}
finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
