$ErrorActionPreference = "Stop"
Get-Content "C:\Users\csfri\source\repos\FCA\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
$params = "start=2026-09-21T13:30:00Z&end=2026-09-21T13:31:00Z&limit=10000&sort=asc&symbols=SPY&feed=sip"
$url = "https://data.alpaca.markets/v2/stocks/quotes?$params"
$creds = @(
    "APCA-API-KEY-ID: $env:LAKE_PROVIDER__API_KEY",
    "APCA-API-SECRET-KEY: $env:LAKE_PROVIDER__API_SECRET"
)
$cases = @(
    @{ name = "creds+gzip"; headers = @("Accept: application/json", "Accept-Encoding: gzip") + $creds },
    @{ name = "creds+gzip-br"; headers = @("Accept: application/json", "Accept-Encoding: gzip, br") + $creds },
    @{ name = "creds+gzip-nospace"; headers = @("Accept: application/json", "Accept-Encoding: gzip,br") + $creds },
    @{ name = "creds-only"; headers = @("Accept: application/json") + $creds }
)
foreach ($case in $cases) {
    $args = @("-sS", "-D", "-")
    foreach ($header in $case.headers) { $args += @("-H", $header) }
    $args += @("-o", "$env:TEMP\probe.bin", $url)
    $meta = & curl.exe @args 2>$null | Out-String
    $encoding = if ($meta -match '(?i)content-encoding:\s*(\S+)') { $Matches[1] } else { "none" }
    Write-Output ($case.name + " -> encoding=" + $encoding)
}
