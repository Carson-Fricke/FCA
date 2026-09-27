$ErrorActionPreference = "Stop"
Get-Content "C:\Users\csfri\source\repos\FCA\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
$url = "https://data.alpaca.markets/v2/stocks/quotes"
$params = "start=2026-09-21T13:30:00Z&end=2026-09-21T13:31:00Z&limit=10000&sort=asc&symbols=SPY&feed=sip"
$providers = @(
    @{ Accept = "application/json" },
    @{ Accept = "application/json"; "Accept-Encoding" = "gzip" },
    @{ Accept = "application/json"; "Accept-Encoding" = "gzip, br" }
)
foreach ($headers in $providers) {
    $headers["APCA-API-KEY-ID"] = $env:LAKE_PROVIDER__API_KEY
    $headers["APCA-API-SECRET-KEY"] = $env:LAKE_PROVIDER__API_SECRET
    $response = Invoke-WebRequest -Uri "https://data.alpaca.markets/v2/stocks/quotes?$params" -Headers $headers -UseBasicParsing -TimeoutSec 30
    $encoding = $response.Headers["Content-Encoding"]
    $raw = $response.RawContentLength
    Write-Output ("accept-encoding=" + $headers["Accept-Encoding"] + " -> content-encoding=" + ($encoding -join ",") + " bytes=" + $response.RawContentLength)
}
