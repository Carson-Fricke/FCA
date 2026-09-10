# Polyglot tool dispatcher for CLEO evidence gates (ADR-051 / ADR-061).
#
# FCA is Python-first with an in-progress Rust engine under rust/. CLEO's
# tool-resolver keys on a single primaryType, so canonical tools would only
# ever resolve to one language. This dispatcher picks the toolchain(s) that the
# current task actually touched and runs the matching canonical tool.
#
# Invoked by .cleo/project-context.json overrides as:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/cleo-verify-tool.ps1 <tool>
#
# Exit code is 0 only when every applicable check passes.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('test', 'build', 'lint', 'typecheck', 'audit', 'security-scan')]
    [string]$Tool
)

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$cargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
if (Test-Path -LiteralPath $cargoBin) {
    $env:Path = "$cargoBin;$env:Path"
}

function Get-ChangedPaths {
    $set = New-Object System.Collections.Generic.HashSet[string]
    try {
        $base = (git merge-base HEAD master 2>$null)
        if ($LASTEXITCODE -eq 0 -and $base) {
            git diff --name-only "$base..HEAD" 2>$null | ForEach-Object { if ($_) { [void]$set.Add($_) } }
        }
    }
    catch { }
    try { git diff --name-only HEAD 2>$null | ForEach-Object { if ($_) { [void]$set.Add($_) } } }
    catch { }
    try {
        git status --porcelain 2>$null | ForEach-Object {
            if ($_ -and $_.Length -gt 3) { [void]$set.Add(($_.Substring(3)).Trim()) }
        }
    }
    catch { }
    return $set
}

$changed = Get-ChangedPaths
$rustTouched = @($changed | Where-Object { $_ -like 'rust/*' }).Count -gt 0
$pyTouched = @($changed | Where-Object {
        $_ -like 'src/*' -or $_ -like 'tests/*' -or $_ -like 'strategies/*' -or $_ -like '*.py' -or $_ -like 'pyproject.toml'
    }).Count -gt 0

$haveRust = Test-Path -LiteralPath (Join-Path $root 'rust\Cargo.toml')
$havePy = Test-Path -LiteralPath (Join-Path $root 'pyproject.toml')

# No diff detected (e.g. verify run on the integration branch after merge):
# default to the Rust workspace when it exists. Per-task worktrees always have a
# merge-base diff, so language selection there is by changed files.
if (-not $rustTouched -and -not $pyTouched) {
    $rustTouched = $haveRust
    $pyTouched = $false
}

$runRust = $rustTouched -and $haveRust
$runPy = $pyTouched -and $havePy
$exit = 0

if ($runRust) {
    switch ($Tool) {
        'test' { & cargo test --manifest-path rust/Cargo.toml --workspace }
        'build' { & cargo build --manifest-path rust/Cargo.toml --workspace }
        'lint' { & cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings }
        'typecheck' { & cargo check --manifest-path rust/Cargo.toml --workspace }
        'audit' { & cargo audit }
        'security-scan' { & cargo audit }
    }
    if ($LASTEXITCODE -ne 0) { $exit = $LASTEXITCODE }
}

if ($runPy -and $exit -eq 0) {
    switch ($Tool) {
        'test' { & uv run pytest -q }
        'build' { & uv build }
        'lint' { & uv run ruff check . }
        'typecheck' { & uv run ty check }
        'audit' { & uv run pip-audit }
        'security-scan' { & uv run pip-audit }
    }
    if ($LASTEXITCODE -ne 0) { $exit = $LASTEXITCODE }
}

if (-not $runRust -and -not $runPy) {
    Write-Output "cleo-verify-tool: no applicable toolchain for '$Tool'"
}

exit $exit
