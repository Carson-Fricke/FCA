#requires -Version 5.1
<#
.SYNOPSIS
Start (or restart) the local Thesis stack with token-stable credentials.

.DESCRIPTION
Runs scripts/local/dev_tokens.py first so LAKE_API__BEARER_TOKEN,
HARNESS_API_BEARER_TOKEN, THESIS_API__BEARER_TOKEN, and the derived
PUBLIC_THESIS_API_BEARER_TOKEN exist in the ignored .env file, loads that file
into the process environment, stops the selected servers' existing listeners,
builds optimized harness and ingestion worker binaries when their sources changed, and launches them detached with logs under
.local\logs.  Builds run after the old listener stops, so a changed binary is
never blocked by the running executable it replaces.

The restart itself always runs in a WMI-detached worker, never as a descendant
of the calling shell: `-Foreground` schedules that worker and then waits until
every selected listener presents a new process and answers its health
endpoint, so a calling shell or agent tool returns as soon as the restart
completes instead of waiting on long-running servers.

The Cosmos DB emulator runs from pinned vNext EN20260907 over loopback HTTP,
bypassing that release's TLS buffering defect. A named data volume is mounted
at /data so conversations survive restarts (the legacy emulator image does not
persist data). It is recreated on every start while the data volume
is retained, because a stale database socket lock in the container's writable
layer otherwise survives a plain `docker start`.  Docker Desktop is started
automatically when the daemon is not running.

.PARAMETER Servers
Subset of cosmos, harness, thesis, ui to launch. Defaults to all four. The
Cosmos emulator is a dependency of the Thesis API's conversation store, so it
is normally left in the default set. The harness includes local market-data storage and finite local ingestion workers.

.PARAMETER Rotate
Mint fresh managed tokens instead of reusing the values already in .env.

.PARAMETER NoStop
Do not stop existing listeners; launch only.  A rebuild of a running binary
will then fail on Windows because the executable is locked, so stop the server
first when its sources changed.

.PARAMETER EnvFile
Environment file to manage and load. Defaults to <repo>\.env.

.PARAMETER Foreground
Schedule the detached restart worker and wait until every selected server is
live before returning.  The worker still owns the restart, so the calling
shell never parents the servers.

.EXAMPLE
powershell -File scripts/local/start-stack.ps1
#>
[CmdletBinding()]
param(
    [string[]]$Servers = @('cosmos', 'harness', 'thesis', 'ui'),
    [switch]$Rotate,
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
    if ($server -notin @('cosmos', 'harness', 'thesis', 'ui')) {
        throw "Unknown server '$server'"
    }
}

$ports = @{ cosmos = 8080; harness = 8001; thesis = 8000; ui = 5173 }
$cargoExe = (Get-Command cargo -ErrorAction SilentlyContinue).Source

function Get-PortOwners {
    param([int]$Port)

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Test-ServerReady {
    param(
        [string]$Name,
        [int]$Port,
        [int[]]$PreviousOwners
    )

    $owners = Get-PortOwners -Port $Port
    $changed = $owners.Count -gt 0 -and (
        $PreviousOwners.Count -eq 0 -or
        @($owners | Where-Object { $PreviousOwners -notcontains $_ }).Count -gt 0
    )
    if ($Name -eq 'cosmos') {
        if (-not $changed) { return $false }
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$Port/ready" -TimeoutSec 5 | Out-Null
            return $true
        } catch {
            return $false
        }
    }
    if ($Name -eq 'ui') { return $changed }
    if (-not $changed) { return $false }
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Wait-StackReady {
    param(
        [string[]]$Names,
        [hashtable]$Previous,
        [int]$WorkerPid,
        [datetime]$Deadline,
        [string]$WorkerLog
    )

    $pending = [System.Collections.Generic.List[string]]::new()
    foreach ($name in $Names) { [void]$pending.Add($name) }
    $workerGoneAt = $null
    while ($pending.Count -gt 0 -and (Get-Date) -lt $Deadline) {
        foreach ($name in @($pending)) {
            if (Test-ServerReady -Name $name -Port $ports[$name] -PreviousOwners $Previous[$name]) {
                Write-Host "==> $name is ready"
                $pending.Remove($name)
            }
        }
        if ($pending.Count -eq 0) { break }
        if ($null -eq $workerGoneAt -and -not (Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue)) {
            $workerGoneAt = Get-Date
        }
        if ($null -ne $workerGoneAt -and (Get-Date) -gt $workerGoneAt.AddSeconds(30)) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
    foreach ($name in $pending) {
        Write-Warning "$name did not become ready; inspect $WorkerLog"
    }
    return ($pending.Count -eq 0)
}

function Build-NativeServer {
    param([string]$Name)

    if ($Name -eq 'harness') {
        if (-not $cargoExe) { throw 'cargo is required to build the harness binary' }
        $taskToolchain = (Select-String -LiteralPath (Join-Path $repo 'src\harness\rust-toolchain.toml') -Pattern '^channel = "([^"]+)"').Matches[0].Groups[1].Value
        Write-Host '==> Building optimized harness and ingestion worker binaries'
        & $cargoExe "+$taskToolchain" build --release --locked --jobs 2 --manifest-path (Join-Path $repo 'src\harness\Cargo.toml') -p api --bin harness --bin harness-worker
        if ($LASTEXITCODE -ne 0) { throw "cargo build for harness failed (exit $LASTEXITCODE)" }
        & $cargoExe "+$taskToolchain" build --release --locked --jobs 2 --manifest-path (Join-Path $repo 'src\lake\Cargo.toml') -p lake-ingest-worker --bin lake-ingest-worker
        if ($LASTEXITCODE -ne 0) { throw "cargo build for the local ingestion worker failed (exit $LASTEXITCODE)" }
        return
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
    if ($NoStop) { $arguments += '-NoStop' }
    # WMI creates the worker under the system provider instead of the calling
    # shell, so the shell tool can terminate immediately while the detached
    # worker owns the restart; Start-Process would parent the long-running
    # servers under the caller and make it wait for them forever.
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

if ($Foreground -and -not $Worker) {
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
    if ($NoStop) { $arguments += '-NoStop' }
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = ($arguments -join ' ')
        CurrentDirectory = $repo
    }
    if ($created.ReturnValue -ne 0) {
        throw "Could not schedule restart worker (Win32 error $($created.ReturnValue))"
    }
    Write-Host "==> Restart scheduled as detached PID $($created.ProcessId)"
    Write-Host "==> Worker log: $workerLog"
    $previous = @{}
    foreach ($name in $Servers) {
        $previous[$name] = if ($NoStop) { @() } else { Get-PortOwners -Port $ports[$name] }
    }
    $ready = Wait-StackReady -Names $Servers -Previous $previous -WorkerPid $created.ProcessId `
        -Deadline (Get-Date).AddSeconds(900) -WorkerLog $workerLog
    if ($ready) {
        Write-Host '==> All selected servers are live'
    }
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

# EN20261008 cannot query our retained catalog. HTTP bypasses EN20260907's TLS bug.
$cosmosImage = 'mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-EN20260907'
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
        -p 127.0.0.1:8081:8081 -p 127.0.0.1:8080:8080 -p 127.0.0.1:1234:1234 `
        -e PROTOCOL=http `
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

# Every server must find its own bearer in the process environment: the UI is
# compiled against PUBLIC_THESIS_API_BEARER_TOKEN, the Thesis API authenticates
# callers with THESIS_API__BEARER_TOKEN, and the harness and lake validate
# theirs on every request.  A missing token would otherwise surface as a 401
# long after the restart reported success.
$requiredTokens = @()
if ($Servers -contains 'harness') { $requiredTokens += 'HARNESS_API_BEARER_TOKEN' }
if ($Servers -contains 'thesis') { $requiredTokens += 'THESIS_API__BEARER_TOKEN' }
if ($Servers -contains 'ui') { $requiredTokens += 'PUBLIC_THESIS_API_BEARER_TOKEN' }
$missingTokens = @($requiredTokens | Where-Object { -not [Environment]::GetEnvironmentVariable($_) })
if ($missingTokens.Count -gt 0) {
    throw ('the environment is missing required bearer tokens: ' + ($missingTokens -join ', '))
}

# A workstation runs the complete harness, including its catalog and finite local workers.
$harnessLakeBearer = $env:LAKE_API__BEARER_TOKEN
if ($Servers -contains 'harness') {
    $env:HARNESS_MARKET_DATA_MODE = 'local'
    $env:LAKE_API_URL = 'http://127.0.0.1:8001/market-data'
    $env:LAKE_LOCAL_WORKER_EXECUTABLE = Join-Path $repo 'src\lake\target\release\lake-ingest-worker.exe'
    $env:LAKE_LOCAL_PROVIDER_ENV = $EnvFile
}

# Optimized native binaries are rebuilt before launch. Thesis and UI use uv/npm.
$uvExe = (Get-Command uv).Source
$npmExe = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npmExe) { $npmExe = (Get-Command npm).Source }

$specs = @{
    harness = [pscustomobject]@{
        Name = 'harness'; Port = 8001; Match = 'harness'
        FilePath = Join-Path $repo 'src\harness\target\release\harness.exe'
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
    Build-NativeServer -Name $spec.Name
    $out = Join-Path $logDir ("{0}-{1}.out.log" -f $spec.Name, $stamp)
    $err = Join-Path $logDir ("{0}-{1}.err.log" -f $spec.Name, $stamp)
    Write-Host "==> Starting $($spec.Name) on port $($spec.Port)"
    $localLakeBearer = $env:LAKE_API__BEARER_TOKEN
    try {
        if ($name -eq 'harness') { $env:LAKE_API__BEARER_TOKEN = $harnessLakeBearer }
        $proc = Start-Process -FilePath $spec.FilePath -ArgumentList $spec.Arguments -WorkingDirectory $repo -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    } finally {
        $env:LAKE_API__BEARER_TOKEN = $localLakeBearer
    }
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
