# Windows 11 / PowerShell installer.  Right-click > Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File install.ps1 [-With speaker,audio] [-All] [-Models]
param([string]$With = "recommended", [switch]$All, [switch]$Models, [switch]$Lock)
$ErrorActionPreference = "Stop"
$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) { $python = "py -3" } elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" } else {
  Write-Host "Python 3.10+ not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') or: winget install Python.Python.3.11"; exit 1 }
$args2 = @("scripts/install.py", "--with", $With)
if ($All) { $args2 += "--all" }; if ($Models) { $args2 += "--models" }; if ($Lock) { $args2 += "--lock" }
Invoke-Expression "$python $($args2 -join ' ')"
Write-Host "`nffmpeg: if 'clipper doctor' says MISS ffmpeg, run: winget install Gyan.FFmpeg   (or rely on the bundled imageio-ffmpeg the installer adds)"
