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

The Cosmos DB emulator runs from the vNext image with a named data volume
mounted at /data so conversations survive restarts (the legacy emulator image
does not persist data).  It is recreated on every start while the data volume
is retained, because a stale database socket lock in the container's writable
layer otherwise survives a plain `docker start`.  Docker Desktop is started
automatically when the daemon is not running.

.PARAMETER Servers
Subset of cosmos, lake, harness, thesis, ui to launch. Defaults to all five. The
Cosmos emulator is a dependency of the Thesis API's conversation store, so it
is normally left in the default set. The market-lake API serves the harness's
on-demand data ensure calls, so it is normally left in the default set too.

.PARAMETER Rotate
Mint fresh managed tokens instead of reusing the values already in .env.

.PARAMETER LakeDebug
Build and launch the market-lake debug binary instead of the release binary.
The lake always runs an optimized build otherwise, because the debug parse
path is several times slower on real quote volumes.

.PARAMETER NoStop
Do not stop existing listeners; launch only.

.PARAMETER EnvFile
Environment file to manage and load. Defaults to <repo>\.env.

.PARAMETER Foreground
Perform the restart in this process and wait for health checks. By default the
script schedules a detached worker and returns before stopping Thesis, so a
live chat request can finish and flush its response.

.EXAMPLE
powershell -File scripts/local/start-stack.ps1
#>
[CmdletBinding()]
param(
    [string[]]$Servers = @('cosmos', 'lake', 'harness', 'thesis', 'ui'),
    [switch]$Rotate,
    [switch]$LakeDebug,
    [switch]$NoStop,
    [string]$EnvFile,
    [switch]$Foreground,
    [switch]$Worker,
    [string]$ServerList,
    [string]$WorkerLog,
    [ValidateRange(0, 30)]
    [int]$DelaySeconds = 3
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $EnvFile) { $EnvFile = Join-Path $repo '.env' }
$logDir = Join-Path $repo '.local\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

if ($ServerList) {
    $Servers = @($ServerList.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
$Servers = @(
    $Servers |
        ForEach-Object { $_.Split(',') } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)
foreach ($server in $Servers) {
    if ($server -notin @('cosmos', 'lake', 'harness', 'thesis', 'ui')) {
        throw "Unknown server '$server'"
    }
}

if ($Worker -and $WorkerLog) {
    Start-Transcript -Path $WorkerLog -Append | Out-Null
}

if (-not $Foreground -and -not $Worker) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $workerLog = Join-Path $logDir "restart-$stamp.log"
    $arguments = @(
        'powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
        '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $PSCommandPath),
        '-Worker', '-ServerList', ('"{0}"' -f ($Servers -join ',')),
        '-EnvFile', ('"{0}"' -f $EnvFile),
        '-WorkerLog', ('"{0}"' -f $workerLog),
        '-DelaySeconds', [string]$DelaySeconds
    )
    if ($Rotate) { $arguments += '-Rotate' }
    if ($LakeDebug) { $arguments += '-LakeDebug' }
    if ($NoStop) { $arguments += '-NoStop' }
    # Start-Process keeps the worker in OpenCode's descendant process tree,
    # which makes the shell tool wait for long-running server grandchildren.
    # WMI creates the worker under the system provider instead, so this command
    # can terminate immediately while the detached worker owns the restart.
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = ($arguments -join ' ')
        CurrentDirectory = $repo
    }
    if ($created.ReturnValue -ne 0) {
        throw "Could not schedule restart worker (Win32 error $($created.ReturnValue))"
    }
    Write-Host "==> Restart scheduled as detached PID $($created.ProcessId)"
    Write-Host "==> Worker log: $workerLog"
    return
}

if ($Worker -and $DelaySeconds -gt 0) {
    Start-Sleep -Seconds $DelaySeconds
}

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

# The vNext Cosmos DB emulator persists to /data; the legacy image does not.
$cosmosImage = 'mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-latest'
$cosmosContainer = 'cosmos-emulator'
$cosmosVolume = 'cosmos-emulator-data'
$cosmosReadyUrl = 'http://127.0.0.1:8080/ready'

function Test-DockerDaemon {
    # A native command's stderr becomes a terminating error under
    # $ErrorActionPreference='Stop' in PowerShell 5.1, so probe with a local
    # Continue preference and discard both streams before checking the exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Start-DockerDaemon {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (Test-Path -LiteralPath $desktop) {
        Write-Host '==> Starting Docker Desktop'
        Start-Process -FilePath $desktop | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(180)
    do {
        Start-Sleep -Seconds 5
    } while (-not (Test-DockerDaemon) -and (Get-Date) -lt $deadline)
    if (-not (Test-DockerDaemon)) {
        throw 'the Docker daemon is not running and could not be started'
    }
}

function Ensure-CosmosEmulator {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'docker is required to run the Cosmos DB emulator'
    }
    if (-not (Test-DockerDaemon)) { Start-DockerDaemon }

    # Recreate the container so its writable layer (and the postgres socket lock
    # in /socket) is clean; only /data is persistent. The image's own start
    # script cannot clear a stale postmaster.pid left in /data/db by an unclean
    # shutdown, so remove it from the volume before booting.
    $nameFilter = 'name=^/{0}$' -f $cosmosContainer
    if (@(docker ps -a --filter $nameFilter --format '{{.Names}}').Count -gt 0) {
        Write-Host "==> Removing existing $cosmosContainer (data volume retained)"
        docker rm -f $cosmosContainer | Out-Null
    }
    Write-Host "==> Creating $cosmosContainer on port 8081"
    docker volume create $cosmosVolume | Out-Null
    docker run --rm --entrypoint /bin/sh -v "${cosmosVolume}:/data" $cosmosImage -c 'rm -f /data/db/postmaster.pid' | Out-Null
    docker run -d --name $cosmosContainer `
        -p 8081:8081 -p 8080:8080 -p 1234:1234 `
        -e PROTOCOL=https `
        -v "${cosmosVolume}:/data" `
        $cosmosImage | Out-Null

    $deadline = (Get-Date).AddSeconds(240)
    do {
        Start-Sleep -Seconds 5
        try {
            Invoke-RestMethod -Uri $cosmosReadyUrl -TimeoutSec 5 | Out-Null
            Write-Host '==> Cosmos DB emulator ready'
            return
        } catch {
            # Not ready yet; keep polling until the deadline.
        }
    } while ((Get-Date) -lt $deadline)
    throw "the Cosmos DB emulator did not become ready; inspect 'docker logs $cosmosContainer'"
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

# The harness no longer embeds CPython: the harness and lake products are
# native Rust binaries launched directly from their build outputs. Thesis and
# the UI still run through uv/npm. The market-lake always launches its release
# build unless -LakeDebug is passed: the debug parse path is several times
# slower on real quote volumes.
$uvExe = (Get-Command uv).Source
$npmExe = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npmExe) { $npmExe = (Get-Command npm).Source }

$lakeExe = Join-Path $repo 'src\lake\target\debug\lake.exe'
if (-not $LakeDebug) {
    $cargoExe = (Get-Command cargo -ErrorAction SilentlyContinue).Source
    if (-not $cargoExe) {
        throw 'cargo is required to build the release market-lake binary'
    }
    Write-Host '==> Building the market-lake release binary'
    & $cargoExe build --release --manifest-path (Join-Path $repo 'src\lake\Cargo.toml') -p lake-cli
    if ($LASTEXITCODE -ne 0) { throw "cargo build --release -p lake-cli failed (exit $LASTEXITCODE)" }
    $lakeExe = Join-Path $repo 'src\lake\target\release\lake.exe'
}

$specs = @{
    lake = [pscustomobject]@{
        Name = 'lake'; Port = 8002; Match = 'lake'
        FilePath = $lakeExe
        Arguments = @('local', 'api', '--host', '127.0.0.1', '--port', '8002')
    }
    harness = [pscustomobject]@{
        Name = 'harness'; Port = 8001; Match = 'harness'
        FilePath = Join-Path $repo 'src\harness\target\debug\harness.exe'
        Arguments = @('api', '--config', 'config/harness/api.yaml')
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

if ($Servers -contains 'cosmos') {
    Ensure-CosmosEmulator
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$started = @()
foreach ($name in $Servers) {
    if (-not $specs.ContainsKey($name)) { continue }
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

if ($Servers -contains 'cosmos') {
    try {
        $ready = Invoke-RestMethod -Uri $cosmosReadyUrl -TimeoutSec 5
        Write-Host ("cosmos /ready: {0}" -f ($ready | ConvertTo-Json -Compress))
    } catch {
        Write-Warning ("cosmos /ready failed: {0}" -f $_.Exception.Message)
    }
}

$started | Format-Table Server, Port, LauncherPid, Out, Err -AutoSize | Out-String | Write-Host

if ($Worker -and $WorkerLog) {
    Stop-Transcript | Out-Null
}
