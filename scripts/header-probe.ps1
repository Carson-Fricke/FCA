$ErrorActionPreference = "Stop"
Get-Content "C:\Users\csfri\source\repos\FCA\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
$params = "start=2026-09-21T13:30:00Z&end=2026-09-21T13:31:00Z&limit=10000&sort=asc&symbols=SPY&feed=sip"
$url = "https://data.alpaca.markets/v2/stocks/quotes?$params"

$cases = @(
    @{ name = "encoding-only"; headers = @("Accept-Encoding: gzip") },
    @{ name = "accept-json+encoding"; headers = @("Accept: application/json", "Accept-Encoding: gzip") },
    @{ name = "full-client-set"; headers = @("Accept: application/json", "Accept-Encoding: gzip, br", "APCA-API-KEY-ID: $env:LAKE_PROVIDER__API_KEY", "APCA-API-SECRET-KEY: $env:LAKE_PROVIDER__API_SECRET") },
    @{ name = "accept-wildcard"; headers = @("Accept: application/json", "Accept-Encoding: gzip, deflate, br") }
)
foreach ($case in $cases) {
    $args = @("-sS", "-D", "-", "-o", "NUL")
    foreach ($header in $case.headers) { $args += @("-H", $header) }
    $args += @("-o", "$env:TEMP\probe.bin", $url)
    $meta = & curl.exe @args 2>$null | Out-String
    $encoding = if ($meta -match '(?i)content-encoding:\s*(\S+)') { $Matches[1] } else { "none" }
    Write-Output ($case.name + " -> encoding=" + $encoding)
}
