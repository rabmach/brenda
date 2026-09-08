# brenda installer for Windows - no packages, no exe: python + this repo.
# Needs python 3.8+ on PATH (python.org installer, or: winget install python)
# Run from the cloned repo folder with:
#   install.cmd
# or: powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Two routes, chosen for you:
#   1. NATIVE - NTFS / FAT / exFAT drives: brenda runs on Windows directly.
#   2. WSL2   - ext4 backup drives: real read-write ext4 inside WSL2.
#      If WSL2 is already installed this is wired up automatically.
#      If not, you get the one-time command (it needs a reboot, so it asks
#      instead of doing it behind your back).
# NOTE: this file is intentionally pure ASCII - Windows PowerShell 5.1
#       misreads UTF-8 scripts without a BOM.

$ErrorActionPreference = "Stop"

# --- python check + native route -------------------------------------------
# resolve an interpreter: "python", else the "py" launcher (which is on PATH
# even when python.exe is not)
$pyExe = $null
$pyRun = @()
foreach ($cand in @(@("python"), @("py", "-3"))) {
    $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
    if ($cmd) { $pyExe = $cand[0]; $pyRun = @($cand | Select-Object -Skip 1); break }
}
if (-not $pyExe) {
    Write-Host ""
    Write-Host "python is not installed (or not on PATH). brenda needs python 3.8+."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        $ans = Read-Host "Install python now via winget? (Y/n)"
        if ($ans -match "^[Yy]?$") {
            winget install -e --id Python.Python.3.13 --override "/quiet InstallAllUsers=0 PrependPath=1"
            if ($LASTEXITCODE -eq 0) {
                Write-Host ""
                Write-Host "python installed. Close this window and double-click"
                Write-Host "install.cmd again (PATH refreshes only for NEW terminals)."
                exit 0
            }
        }
    } else {
        Write-Host "winget is not available on this Windows - install python by hand:"
        Write-Host "  1. download: https://www.python.org/downloads/windows/"
        Write-Host "  2. in the installer tick 'Add python.exe to PATH'"
        Write-Host "  3. close this window and run install.cmd again"
        exit 1
    }
}
$v = & $pyExe @pyRun --version 2>&1
Write-Host "python found: $v (via $pyExe)"

Push-Location $PSScriptRoot
try {
    # everything the installer prints also lands in install.log - pasteable
    & $pyExe @pyRun .\brenda self-test 2>&1 | Tee-Object -FilePath install.log
    if ($LASTEXITCODE -ne 0) { Write-Error "self-test failed - full output is in brenda\install.log"; exit 1 }

    $shim = Join-Path $PSScriptRoot "brenda.cmd"
    $pyCall = ("$pyExe " + ($pyRun -join " ")).Trim()
    Set-Content -Path $shim -Value "@echo off`r`n$pyCall `"%~dp0brenda`" %*" -Encoding ASCII
    Write-Host ""
    Write-Host "wrote $shim"
    Write-Host "native use (NTFS/FAT/exFAT):  .\brenda.cmd scan D:\    .\brenda.cmd serve"
    Write-Host "data home: %LOCALAPPDATA%\brenda"

    # --- WSL2 route for ext4 drives -----------------------------------------
    $wslOk = $false
    try {
        wsl --status *> $null
        if ($LASTEXITCODE -eq 0) { $wslOk = $true }
    } catch {}
    if ($wslOk) {
        Write-Host ""
        Write-Host "WSL2 detected - wiring brenda inside the default distro..."
        wsl -e bash -lc "command -v python3 >/dev/null 2>&1 || { echo 'the WSL distro has no python3 - run: sudo apt install python3'; exit 1; }; test -d $HOME/brenda/.git || git clone https://github.com/rabmach/brenda.git $HOME/brenda; cd $HOME/brenda && git pull -ff 2>/dev/null; python3 brenda self-test"
        if ($LASTEXITCODE -eq 0) {
            $shim2 = Join-Path $PSScriptRoot "brenda-wsl.ps1"
            Set-Content -Path $shim2 -Encoding UTF8 -Value @'
# brenda-wsl: plug in the ext4 drive and run this.
#   powershell -ExecutionPolicy Bypass -File .\brenda-wsl.ps1
# Automagic: finds ONE USB disk (asks only if several are plugged),
# mounts it into WSL2 read-write, starts brenda serve inside WSL,
# opens the dashboard on the Windows side. Stop: close the WSL window,
# then:  wsl --unmount \\.\PHYSICALDRIVE<n>
$ErrorActionPreference = "Stop"
where.exe wsl *> $null
if ($LASTEXITCODE -ne 0) { Write-Error "WSL2 required: wsl --install --no-launch, reboot, re-run install.ps1"; exit 1 }
$usb = @(Get-CimInstance Win32_DiskDrive |
    Where-Object { $_.InterfaceType -eq "USB" -or $_.Model -match "USB" })
if (-not $usb) { Write-Error "no USB drive found - plug in the ext4 drive and retry"; exit 1 }
$disk = $usb[0]                                  # one drive = zero questions
if ($usb.Count -gt 1) {
    Write-Host "several USB drives found:"
    $i = 0
    foreach ($d in $usb) { $i++; Write-Host ("  {0}: {1} ({2:N0} GB)" -f $i, $d.FriendlyName, ($d.Size / 1GB)) }
    $pick = Read-Host "pick 1-$i"
    $disk = $usb[[int]$pick - 1]
}
$dev = $disk.DeviceID                            # \\.\PHYSICALDRIVE<n>
Write-Host "mounting $dev into WSL2 (read-write ext4)..."
wsl --mount $dev
if ($LASTEXITCODE -ne 0) { Write-Error "wsl --mount failed - is the drive attached and healthy?"; exit 1 }
Write-Host "starting brenda serve inside WSL (dashboard opens on Windows)..."
Start-Process wsl -ArgumentList "-e", "bash", "-lc", "cd $HOME/brenda && python3 brenda serve --no-open"
Start-Sleep 6
$state = (wsl -e bash -lc "cat $HOME/.local/share/brenda/serve.state.json 2>/dev/null") -join "`n"
$url = [regex]::Match($state, "http://127\.0\.0\.1:\d+/[a-f0-9]+/").Value
if ($url) { Start-Process $url }
else { Write-Host "could not read the dashboard URL - open the one printed in the WSL window" }
Write-Host "brenda is live. When done: close the WSL window, then: wsl --unmount $dev"
'@
            Write-Host "wrote $shim2 - plug in the ext4 drive and run:"
            Write-Host "    powershell -ExecutionPolicy Bypass -File .\brenda-wsl.ps1"
        }
    } else {
        Write-Host ""
        Write-Host "WSL2 not installed. For your ext4 backup drives, the one-time setup"
        Write-Host "(it needs a reboot, so it asks instead of just doing it):"
        Write-Host "    wsl --install --no-launch      # reboot, create your Linux user,"
        Write-Host "                                   # then re-run this script"
        Write-Host "brenda will be wired inside WSL automatically after that."
    }
} finally {
    Pop-Location
}
