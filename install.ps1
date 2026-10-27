# Grid CLI installer for Windows.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://grid.autonomous.ai/install.ps1 | iex"
#
# The Windows twin of install.sh's wheel path (the one macOS takes): the universal release wheel,
# installed with uv, gives you `grid` (+ the `agrid` alias). Two things are Windows-only:
#   - grid runs on a SIGNED Python (python.org's, through winget when none is installed), never on the
#     CPython builds uv downloads: those are unsigned, and Smart App Control blocks unsigned binaries,
#     which leaves a grid that cannot even import ssl (measured on Windows 11, enforce mode).
#   - nothing here calls `exit`: under `irm | iex` this runs in the caller's own session, and an exit
#     would close their PowerShell window. Failures `throw` instead.
#
# Knobs (all optional, as environment variables):
#   GRID_VERSION=0.1.0        pin a version (default: latest release)
#   GRID_WHEEL_URL / GRID_PACKAGE   this wheel URL / any requirement `uv tool install` takes. Not a bare
#                                   PyPI name: `grid` on PyPI is an unrelated package.
#   GRID_REPO_OWNER / _NAME   source repo (default: autonomous-ai / autonomous-grid)
#   GRID_PYTHON=C:\...\python.exe   the interpreter to install grid on (default: found or installed)
#
# Kept to ASCII: Windows PowerShell 5.1 reads a script file without a BOM as the ANSI code page.

& {
# 'Continue', not 'Stop': Windows PowerShell 5.1 turns a native command's redirected stderr into
# terminating errors under 'Stop', so probing a missing `py -3.13`, or uv's own progress lines, would
# abort the install. Every step below checks $LASTEXITCODE (or the result) itself, and Fail throws.
$ErrorActionPreference = 'Continue'
# Windows PowerShell 5.1 can still default to TLS 1.0, which github.com refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Owner = if ($env:GRID_REPO_OWNER) { $env:GRID_REPO_OWNER } else { 'autonomous-ai' }
$Repo = if ($env:GRID_REPO_NAME) { $env:GRID_REPO_NAME } else { 'autonomous-grid' }
$Version = if ($env:GRID_VERSION) { $env:GRID_VERSION } else { 'latest' }

function Say([string]$Message) { Write-Host ">>> $Message" -ForegroundColor Cyan }
function Ok([string]$Message) { Write-Host "OK  $Message" -ForegroundColor Green }
function Fail([string]$Message) { throw "ERROR: $Message" }

# The newest CLI release tag WITHOUT api.github.com (60 unauthenticated requests per hour per IP, so a
# shared office address 403s): the redirect of /releases/latest first, the Atom feed second. Only a CLI
# tag (`v<digit>...`) counts: this repository also publishes `protocol-v...` releases.
function Get-LatestReleaseTag {
    try {
        $request = [System.Net.HttpWebRequest]::Create("https://github.com/$Owner/$Repo/releases/latest")
        $request.AllowAutoRedirect = $false
        $request.Method = 'HEAD'
        $response = $request.GetResponse()
        $location = $response.Headers['Location']
        $response.Close()
        if ($location) {
            $tag = ($location -split '/')[-1] -replace '\?.*$', ''
            if ($tag -match '^v[0-9]') { return $tag }
        }
    } catch { }
    try {
        $feed = (Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Owner/$Repo/releases.atom").Content
        $match = [regex]::Match($feed, '/releases/tag/(v[0-9][^"<]*)')
        if ($match.Success) { return $match.Groups[1].Value }
    } catch { }
    return $null
}

# A Python >= 3.11 that can import ssl: the check that fails first when Smart App Control has
# blocked an interpreter's unsigned DLLs.
function Test-Python([string]$Exe) {
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    if ($Exe -like '*\WindowsApps\*') { return $false }  # the Microsoft Store alias stub, not a Python
    & $Exe -c "import sys, ssl; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    if ($env:GRID_PYTHON) {
        if (Test-Python $env:GRID_PYTHON) { return $env:GRID_PYTHON }
        Fail "GRID_PYTHON is not a working Python 3.11+: $env:GRID_PYTHON"
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($minor in '3.13', '3.12', '3.11') {
            $exe = & py "-$minor" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and (Test-Python "$exe".Trim())) { return "$exe".Trim() }
        }
    }
    foreach ($command in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if (Test-Python $command.Source) { return $command.Source }
    }
    foreach ($minor in '313', '312', '311') {
        $exe = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$minor\python.exe"
        if (Test-Python $exe) { return $exe }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "no Python 3.11+ found and winget is not available. Install Python 3.12 from https://www.python.org/downloads/windows/ and run this again."
    }
    Say 'Installing Python 3.12 from python.org (signed, for this account only)...'
    & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Python $python)) { Fail "Python 3.12 did not install cleanly at $python. Install it from https://www.python.org/downloads/windows/ and run this again." }
}
Ok "Python: $python"

$env:PATH = "$HOME\.local\bin;$env:PATH"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'Installing uv (Python package manager)...'
    & powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
    $env:PATH = "$HOME\.local\bin;$env:PATH"
}
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { Fail 'uv is not on PATH after installing it - open a new terminal and run this again.' }

if ($env:GRID_WHEEL_URL) {
    $source = $env:GRID_WHEEL_URL
} elseif ($env:GRID_PACKAGE) {
    $source = $env:GRID_PACKAGE
} else {
    if ($Version -eq 'latest') {
        $tag = Get-LatestReleaseTag
        if (-not $tag) { Fail 'could not resolve the latest grid release - set GRID_VERSION=X.Y.Z or GRID_WHEEL_URL=...' }
        $ver = $tag.Substring(1)
    } else {
        $ver = $Version
        $tag = "v$Version"
    }
    Say "Resolving grid wheel from $Owner/$Repo ($Version -> $tag)..."
    $source = "https://github.com/$Owner/$Repo/releases/download/$tag/grid-$ver-py3-none-any.whl"
}

# A grid already running from this install holds its files open: `uv tool install --force` would then
# fail half-way, removing the packages but not the locked Scripts folder, and leave a grid that cannot
# even `grid stop` (measured on Windows 11). So look before replacing anything.
$toolRoot = (& $uv tool dir).Trim()
$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$toolRoot\grid\", [StringComparison]::OrdinalIgnoreCase)
})
if ($running.Count -gt 0) {
    Fail ("grid is running from this install (pid " + (($running | ForEach-Object { $_.ProcessId }) -join ', ') +
          "). Stop it first - 'grid leave --all' for engines, 'grid stop' for the grid - then run this again.")
}

Say 'Installing grid...'
& $uv tool install --force --python $python $source | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "uv tool install failed for $source" }

$binDir = (& $uv tool dir --bin).Trim()
$grid = Join-Path $binDir 'grid.exe'
if (-not (Test-Path -LiteralPath $grid)) { Fail "installed, but $grid is missing" }

# uv's grid.exe is unsigned and rewritten per install, and Smart App Control blocks some installs' copy
# outright (measured on Windows 11). The install's own python.exe is signed by the PSF, so when Windows
# refuses the .exe, grid/agrid become .cmd launchers that run it - and the blocked .exe is moved aside,
# or `grid` would still resolve to it (.EXE comes before .CMD in PATHEXT). A .exe that runs is kept,
# and launchers left from an earlier blocked install are removed.
$blocked = $false
try { & $grid --version *> $null } catch { $blocked = ("$_" -match 'Application Control') }
$venvPython = Join-Path $toolRoot 'grid\Scripts\python.exe'
foreach ($name in 'grid', 'agrid') {
    $exe = Join-Path $binDir "$name.exe"
    $cmd = Join-Path $binDir "$name.cmd"
    $aside = Join-Path $binDir "$name.exe.blocked"
    if ($blocked) {
        # npm cmd-shim's form: ending the batch context before python runs means Ctrl+C never asks
        # "Terminate batch job (Y/N)?" (measured against a plain .cmd).
        $launch = 'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "' + $venvPython + '" -m cli %*'
        [IO.File]::WriteAllLines($cmd, @('@ECHO off', 'SETLOCAL', $launch), [Text.Encoding]::ASCII)
        if (Test-Path -LiteralPath $exe) { Move-Item -Force -LiteralPath $exe -Destination $aside }
    } else {
        Remove-Item -Force -LiteralPath $cmd, $aside -ErrorAction SilentlyContinue
    }
}
if ($blocked) {
    $grid = Join-Path $binDir 'grid.cmd'
    Say 'Windows (Smart App Control) blocked grid.exe; grid starts through its signed Python instead.'
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (($userPath -split ';') -contains $binDir)) {
    Say "Add $binDir to PATH:  uv tool update-shell   (then open a new terminal)"
}

$installed = & $grid --version 2>&1
if ($LASTEXITCODE -ne 0) { Fail "installed but failed to run: $installed" }
Ok "$installed"
Say 'Next:  grid login       # sign in, then pick from the grids you can use'
# A new install starts in remote mode, where 'grid start' means "start my hosted grid" and asks for a
# sign-in. So the local path has to name the mode, exactly as install.sh says it.
Say "  or:  grid mode local  # then 'grid start' creates your own grid on this computer"
}
