param(
    [string]$Version = "0.2.1"
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$ReleaseDir = Join-Path $Root "release"
$BuildIcon = Join-Path $Root "build\whisper_lm.ico"

Write-Host "Ensuring PyInstaller is installed..."
& $Python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install pyinstaller" }
}
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path (Join-Path $Root "build"), $ReleaseDir | Out-Null

Write-Host "Generating WhisperLM multi-resolution icon..."
& $Python -c "
import struct
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication
from whisper_lm.ui import render_brand_pixmap

app = QApplication([])
sizes = [16, 24, 32, 48, 64, 128, 256]
frames = []
for s in sizes:
    pix = render_brand_pixmap(s, '#F5F5F7')
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, 'PNG')
    frames.append((s, bytes(buf.data())))

header = struct.pack('<HHH', 0, 1, len(frames))
offset = 6 + 16 * len(frames)
entries = bytearray()
blobs = bytearray()
for s, data in frames:
    dim = 0 if s >= 256 else s
    entries += struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
    blobs += data

with open(r'$BuildIcon', 'wb') as f:
    f.write(header + bytes(entries) + bytes(blobs))
"

$EntryScript = Join-Path $Root "build\launcher.py"
Set-Content -Path $EntryScript -Encoding UTF8 -Value @"
from whisper_lm.app import main

if __name__ == "__main__":
    raise SystemExit(main())
"@

Write-Host "Packaging WhisperLM.exe with PyInstaller..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "WhisperLM" `
    --icon $BuildIcon `
    --paths (Join-Path $Root "src") `
    --collect-all onnxruntime `
    --collect-all sounddevice `
    --collect-all pyaudiowpatch `
    --exclude-module PySide6.QtWebEngine `
    --exclude-module PySide6.QtWebEngineCore `
    --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtPdf `
    --exclude-module PySide6.QtPdfWidgets `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtQuickWidgets `
    --exclude-module PySide6.Qt3DCore `
    --exclude-module PySide6.Qt3DRender `
    --exclude-module PySide6.QtMultimedia `
    --exclude-module PySide6.QtMultimediaWidgets `
    --exclude-module tkinter `
    --exclude-module unittest `
    $EntryScript

$AppDist = Join-Path $Root "dist\WhisperLM"
if (-not (Test-Path (Join-Path $AppDist "WhisperLM.exe"))) {
    throw "PyInstaller build failed: WhisperLM.exe not found."
}

Write-Host "Bundling Vulkan compute engines and Silero VAD..."
Copy-Item -Recurse -Force (Join-Path $Root "bin") (Join-Path $AppDist "bin")
New-Item -ItemType Directory -Force -Path (Join-Path $AppDist "models") | Out-Null
Copy-Item -Force (Join-Path $Root "models\silero_vad.onnx") (Join-Path $AppDist "models\silero_vad.onnx")
Copy-Item -Force (Join-Path $Root "README.md") (Join-Path $AppDist "README.md")

$ZipName = "WhisperLM-Windows-x64.zip"
$ZipPath = Join-Path $ReleaseDir $ZipName
if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
}

Write-Host "Creating compressed release archive $ZipPath..."
Compress-Archive -Path "$AppDist\*" -DestinationPath $ZipPath -CompressionLevel Optimal
$SizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host "Built $ZipPath ($SizeMB MB)"

$IsccCandidates = @(
    "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { $Iscc = $cmd.Source }
}
$IssFile = Join-Path $PSScriptRoot "installer.iss"
if ($Iscc -and (Test-Path $IssFile)) {
    Write-Host "Building standalone Windows installer with Inno Setup..."
    & $Iscc "/DMyAppVersion=$Version" $IssFile
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compilation failed with exit code $LASTEXITCODE"
    }
    $SetupPath = Join-Path $ReleaseDir "WhisperLM-Setup.exe"
    $SetupMB = [math]::Round((Get-Item $SetupPath).Length / 1MB, 1)
    Write-Host "Built $SetupPath ($SetupMB MB)"
} else {
    Write-Warning "Inno Setup (ISCC.exe) not found; skipping WhisperLM-Setup.exe build."
}
