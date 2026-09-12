#requires -Version 5.1
<#
.SYNOPSIS
Start (or restart) the local Thesis stack with token-stable credentials.

.DESCRIPTION
Runs scripts/local/dev_tokens.py first so LAKE_API__BEARER_TOKEN,
HARNESS_API_BEARER_TOKEN, THESIS_API__BEARER_TOKEN, and the derived
PUBLIC_THESIS_API_BEARER_TOKEN exist in the ignored .env file, loads that file
into the process environment, then launches the selected servers detached with
logs under .local/logs.  Existing listeners on the target ports are stopped
first when their command line matches the expected server.

.PARAMETER Servers
Subset of harness, thesis, ui to launch. Defaults to all three.

.PARAMETER Rotate
Mint fresh managed tokens instead of reusing the values already in .env.

.PARAMETER NoStop
Do not stop existing listeners; launch only.

.PARAMETER EnvFile
Environment file to manage and load. Defaults to <repo>\.env.

.EXAMPLE
powershell -File scripts/local/start-stack.ps1
#>
[CmdletBinding()]
param(
    [ValidateSet('harness', 'thesis', 'ui')]
    [string[]]$Servers = @('harness', 'thesis', 'ui'),
    [switch]$Rotate,
    [switch]$NoStop,
    [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $EnvFile) { $EnvFile = Join-Path $repo '.env' }
$logDir = Join-Path $repo '.local\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Stop-ListeningServer {
    param([int]$Port, [string]$Match)

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) { return }

    foreach ($ownerPid in ($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if ($proc.CommandLine -and $proc.CommandLine -match $Match) {
            & taskkill.exe /PID $ownerPid /T /F | Out-Null
        } else {
            throw "Port $Port is held by an unexpected process (${ownerPid}: $($proc.Name)); free it and retry"
        }
    }

    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 400
        $remaining = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    } while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)
    if ($remaining.Count -gt 0) { throw "Port $Port did not free after stopping the previous server" }
}

Write-Host '==> Ensuring development bearer tokens'
$helperArgs = @('run', '--no-sync', 'python', 'scripts/local/dev_tokens.py', '--env-file', $EnvFile)
if ($Rotate) { $helperArgs += '--rotate' }
& uv @helperArgs
if ($LASTEXITCODE -ne 0) { throw "dev_tokens.py failed (exit $LASTEXITCODE)" }

Write-Host '==> Loading environment'
Get-Content -LiteralPath $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line.Length -eq 0 -or $line.StartsWith('#')) { return }
    $index = $line.IndexOf('=')
    if ($index -lt 1) { return }
    $name = $line.Substring(0, $index).Trim()
    if ($name -like 'export *') { $name = $name.Substring(7).Trim() }
    $value = $line.Substring($index + 1)
    Set-Item -LiteralPath ("Env:{0}" -f $name) -Value $value
}

# The native harness facade embeds CPython and loads python312.dll at startup,
# so the base interpreter directory must be on PATH for the child process.
$pythonBase = & uv run --no-sync python -c "import sys; print(sys.base_prefix)"
if ($LASTEXITCODE -eq 0 -and $pythonBase) { $env:PATH = "$pythonBase;$env:PATH" }

$uvExe = (Get-Command uv).Source
$npmExe = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npmExe) { $npmExe = (Get-Command npm).Source }

$specs = @{
    harness = [pscustomobject]@{
        Name = 'harness'; Port = 8001; Match = 'harness'
        FilePath = $uvExe
        Arguments = @('run', '--no-sync', 'harness', 'api', '--config', 'config/harness/api.yaml')
    }
    thesis = [pscustomobject]@{
        Name = 'thesis'; Port = 8000; Match = 'thesis_local_api'
        FilePath = $uvExe
        Arguments = @('run', '--no-sync', 'python', 'scripts/local/thesis_local_api.py')
    }
    ui = [pscustomobject]@{
        Name = 'ui'; Port = 5173; Match = 'vite'
        FilePath = $npmExe
        Arguments = @('run', 'dev', '--prefix', 'src/ui', '--', '--host', '127.0.0.1', '--port', '5173')
    }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$started = @()
foreach ($name in $Servers) {
    $spec = $specs[$name]
    if (-not $NoStop) {
        Write-Host "==> Stopping existing $($spec.Name) on port $($spec.Port)"
        Stop-ListeningServer -Port $spec.Port -Match $spec.Match
    }
    $out = Join-Path $logDir ("{0}-{1}.out.log" -f $spec.Name, $stamp)
    $err = Join-Path $logDir ("{0}-{1}.err.log" -f $spec.Name, $stamp)
    Write-Host "==> Starting $($spec.Name) on port $($spec.Port)"
    $proc = Start-Process -FilePath $spec.FilePath -ArgumentList $spec.Arguments -WorkingDirectory $repo -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    $started += [pscustomobject]@{
        Server = $spec.Name; Port = $spec.Port; LauncherPid = $proc.Id; Out = $out; Err = $err
    }
}

foreach ($entry in $started) {
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        $listen = @(Get-NetTCPConnection -State Listen -LocalPort $entry.Port -ErrorAction SilentlyContinue)
    } while ($listen.Count -eq 0 -and (Get-Date) -lt $deadline)
    if ($listen.Count -eq 0) {
        Write-Warning "$($entry.Server) did not bind port $($entry.Port); see $($entry.Err)"
    }
}

foreach ($entry in $started) {
    if ($entry.Server -eq 'ui') { continue }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($entry.Port)/health" -TimeoutSec 5
        Write-Host ("{0} /health: {1}" -f $entry.Server, ($health | ConvertTo-Json -Compress))
    } catch {
        Write-Warning ("{0} /health failed: {1}" -f $entry.Server, $_.Exception.Message)
    }
}

$started | Format-Table Server, Port, LauncherPid, Out, Err -AutoSize | Out-String | Write-Host
