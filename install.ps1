# brenda installer for Windows — no packages, no exe: python + this repo.
# Needs python 3.8+ on PATH (python.org installer, or: winget install python)
# Use from the cloned repo folder:  powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Two routes, chosen for you:
#   1. NATIVE — NTFS / FAT / exFAT drives: brenda runs on Windows directly.
#   2. WSL2  — ext4 backup drives: real read-write ext4 inside WSL2.
#      If WSL2 is already installed this is wired up automatically.
#      If not, you get the one-time command (it needs a reboot, so it asks
#      instead of doing it behind your back).

$ErrorActionPreference = "Stop"

# --- python check + native route -------------------------------------------
try {
    $v = & python --version 2>&1
} catch {
    Write-Error "python not found on PATH. Install from python.org or: winget install python"
    exit 1
}
Write-Host "python found: $v"

Push-Location $PSScriptRoot
try {
    & python .\brenda self-test
    if ($LASTEXITCODE -ne 0) { Write-Error "self-test failed — see above"; exit 1 }

    $shim = Join-Path $PSScriptRoot "brenda.cmd"
    Set-Content -Path $shim -Value "@echo off`r`npython `"%~dp0brenda`" %*" -Encoding ASCII
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
        Write-Host "WSL2 detected — wiring brenda inside the default distro..."
        wsl -e bash -lc "command -v python3 >/dev/null 2>&1 || { echo 'the WSL distro has no python3 — run: sudo apt install python3'; exit 1; }; test -d $HOME/brenda/.git || git clone https://github.com/rabmach/brenda.git $HOME/brenda; cd $HOME/brenda && git pull -ff 2>/dev/null; python3 brenda self-test"
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
            Write-Host "wrote $shim2 — plug in the ext4 drive and run:"
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
