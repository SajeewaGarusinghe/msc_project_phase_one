<#
.SYNOPSIS
    Checks prerequisites for running the MSc RCA bench locally on Windows.

.DESCRIPTION
    READ-ONLY by default. It inspects the machine and tells you exactly what is
    missing and what to do about it. It changes nothing unless you pass -Clone,
    which only clones the repository into your WSL home directory.

    Run from PowerShell (no admin needed for the checks):
        powershell -ExecutionPolicy Bypass -File .\bootstrap-windows.ps1

.PARAMETER Clone
    After the checks pass, clone the repo into the WSL2 home directory.

.NOTES
    The bench must run INSIDE WSL2, not from D:. See docs/06-windows-setup.md
    for why -- bind mounts of the containerd socket and POSIX permissions do not
    survive the /mnt/d boundary.
#>
[CmdletBinding()]
param(
    [switch]$Clone,
    [string]$Repo   = "https://github.com/SajeewaGarusinghe/msc_project_phase_one.git",
    [string]$Branch = "feat/p0-bench"
)

$ErrorActionPreference = "Stop"
$script:Problems = @()

function Say  ($m) { Write-Host $m }
function Good ($m) { Write-Host "  [ OK ] $m"   -ForegroundColor Green }
function Warn ($m) { Write-Host "  [WARN] $m"   -ForegroundColor Yellow }
function Bad  ($m) { Write-Host "  [FAIL] $m"   -ForegroundColor Red
                     $script:Problems += $m }

Say ""
Say "MSc RCA bench -- Windows prerequisite check"
Say "==========================================="
Say ""

# --- 1. Memory ------------------------------------------------------------
Say "1. Physical memory"
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
if     ($ramGB -ge 32) { Good "$ramGB GB -- comfortable. Allocate 20 GB to WSL." }
elseif ($ramGB -ge 16) { Warn "$ramGB GB -- tight. Allocate 12 GB to WSL and close other apps." }
else                   { Bad  "$ramGB GB -- not enough. The stack needs ~12 GB. Use the cloud bench." }

# --- 2. WSL2 --------------------------------------------------------------
Say ""
Say "2. WSL2"
$script:HasLinuxDistro = $false
if (Get-Command wsl -ErrorAction SilentlyContinue) {
    # wsl.exe emits UTF-16; normalise so -match behaves.
    $distros = (wsl --list --quiet 2>&1 | Out-String) -replace "`0", ""
    $names = $distros -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }

    # docker-desktop is Docker's own helper distro. It is not a usable
    # workspace: it has no apt and no login shell, which is why `wsl -e bash`
    # fails with execvpe(bash) when it is the default.
    $usable = $names | Where-Object { $_ -notmatch '^docker-desktop' }

    if ($names) { Say "       distros present: $($names -join ', ')" }
    else        { Say "       no distros registered" }

    if ($usable) {
        Good "usable Linux distro: $($usable -join ', ')"
        $script:HasLinuxDistro = $true
        $verbose = (wsl --list --verbose 2>&1 | Out-String) -replace "`0", ""
        foreach ($d in $usable) {
            if ($verbose -match [regex]::Escape($d) + '\s+\w+\s+1\b') {
                Bad "$d is on WSL 1 -- run: wsl --set-version $d 2"
            }
        }
    } else {
        Bad "No usable Linux distro (docker-desktop does not count). Run as Administrator: wsl --install -d Ubuntu"
    }
} else {
    Bad "WSL not installed. Run as Administrator: wsl --install -d Ubuntu"
}

# --- 3. .wslconfig memory limit ------------------------------------------
Say ""
Say "3. WSL memory allocation"
$wslConfig = Join-Path $env:USERPROFILE ".wslconfig"
if (Test-Path $wslConfig) {
    $cfg = Get-Content $wslConfig -Raw
    if ($cfg -match 'memory\s*=\s*(\d+)GB') {
        $alloc = [int]$Matches[1]
        if ($alloc -ge 12) { Good ".wslconfig allocates ${alloc}GB" }
        else { Bad ".wslconfig allocates only ${alloc}GB -- the app alone needs ~6GB. Raise to 12GB+." }
    } else {
        Warn ".wslconfig exists but sets no memory limit. WSL defaults to ~50% of RAM."
    }
} else {
    Warn "No .wslconfig. WSL defaults to ~50% of RAM, which may be enough on 32GB."
    Say  "       To set it explicitly, create $wslConfig containing:"
    Say  "           [wsl2]"
    Say  "           memory=16GB"
    Say  "           processors=6"
    Say  "       then run: wsl --shutdown"
}

# --- 4. Docker Desktop ----------------------------------------------------
Say ""
Say "4. Docker"
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try {
        $null = docker info 2>&1
        if ($LASTEXITCODE -eq 0) {
            Good "Docker daemon responding"
            $dockerMem = docker info --format '{{.MemTotal}}' 2>$null
            if ($dockerMem -and $dockerMem -match '^\d+$') {
                $dockerGB = [math]::Round([int64]$dockerMem / 1GB)
                if ($dockerGB -ge 12) { Good "Docker sees ${dockerGB}GB" }
                else { Bad "Docker sees only ${dockerGB}GB -- raise the WSL memory limit (step 3)." }
            }
        } else {
            Bad "Docker is installed but the daemon is not responding. Start Docker Desktop."
        }
    } catch { Bad "Could not query Docker: $_" }
} else {
    Bad "Docker not found. Install Docker Desktop and enable the WSL2 backend."
}

# --- 5. WSL toolchain -----------------------------------------------------
Say ""
Say "5. Toolchain inside WSL"
if (-not $script:HasLinuxDistro) {
    Warn "Skipped -- no usable Linux distro yet (fix step 2 first)"
} else {
    # 2>$null does NOT suppress a native command's stderr in PowerShell; it
    # surfaces as a NativeCommandError. Redirect the whole stream instead.
    $ErrorActionPreference = "Continue"
    foreach ($tool in @("git", "make", "k3d", "kubectl", "helm")) {
        $out = (wsl -e bash -lc "command -v $tool" 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $out -and $out -notmatch 'execvpe|ERROR') {
            Good "$tool present in WSL"
        } else {
            Bad "$tool missing in WSL -- run scripts/setup-wsl.sh (see step 3 of docs/06-windows-setup.md)"
        }
    }
    $ErrorActionPreference = "Stop"
}

# --- Summary --------------------------------------------------------------
Say ""
Say "==========================================="
if ($script:Problems.Count -eq 0) {
    Write-Host "All checks passed." -ForegroundColor Green
} else {
    Write-Host "$($script:Problems.Count) problem(s) to fix:" -ForegroundColor Red
    $script:Problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Say ""
    Say "See docs/06-windows-setup.md for the full walkthrough."
    if ($Clone) { Say "Not cloning while checks are failing."; exit 1 }
    exit 1
}

# --- Optional clone -------------------------------------------------------
if ($Clone) {
    Say ""
    Say "Cloning into the WSL home directory (NOT D: -- see docs/06-windows-setup.md)"
    $script = @"
set -e
cd ~
if [ -d msc_project_phase_one ]; then
  echo 'Repo already exists at ~/msc_project_phase_one -- pulling instead.'
  cd msc_project_phase_one && git fetch origin && git checkout $Branch && git pull
else
  git clone $Repo
  cd msc_project_phase_one && git checkout $Branch
fi
echo
echo 'Ready. Inside WSL run:'
echo '  cd ~/msc_project_phase_one'
echo '  make local-up && make install TARGET=local && make verify TARGET=local'
"@
    wsl -e bash -lc $script
} else {
    Say ""
    Say "Next: re-run with -Clone to fetch the repo into WSL, or clone it yourself:"
    Say "  wsl"
    Say "  git clone $Repo && cd msc_project_phase_one && git checkout $Branch"
}
