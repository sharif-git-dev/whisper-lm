param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$ReleaseDir = Join-Path $Root "release"
$BuildIcon = Join-Path $Root "build\whisper_flow.ico"

Write-Host "Ensuring PyInstaller is installed..."
& $Python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install pyinstaller" }
}
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path (Join-Path $Root "build"), $ReleaseDir | Out-Null

Write-Host "Generating monochrome Whisper Flow icon..."
& $Python -c "
from PySide6.QtWidgets import QApplication
from whisper_flow.ui import _build_tray_icon
app = QApplication([])
icon = _build_tray_icon('#F5F5F7')
pix = icon.pixmap(64, 64)
pix.save(r'$BuildIcon', 'ICO')
"

$EntryScript = Join-Path $Root "build\launcher.py"
Set-Content -Path $EntryScript -Encoding UTF8 -Value @"
from whisper_flow.app import main

if __name__ == "__main__":
    raise SystemExit(main())
"@

Write-Host "Packaging WhisperFlow.exe with PyInstaller..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "WhisperFlow" `
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

$AppDist = Join-Path $Root "dist\WhisperFlow"
if (-not (Test-Path (Join-Path $AppDist "WhisperFlow.exe"))) {
    throw "PyInstaller build failed: WhisperFlow.exe not found."
}

Write-Host "Bundling Vulkan compute engines and Silero VAD..."
Copy-Item -Recurse -Force (Join-Path $Root "bin") (Join-Path $AppDist "bin")
New-Item -ItemType Directory -Force -Path (Join-Path $AppDist "models") | Out-Null
Copy-Item -Force (Join-Path $Root "models\silero_vad.onnx") (Join-Path $AppDist "models\silero_vad.onnx")
Copy-Item -Force (Join-Path $Root "README.md") (Join-Path $AppDist "README.md")

$ZipName = "WhisperFlow-Windows-x64.zip"
$ZipPath = Join-Path $ReleaseDir $ZipName
if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
}

Write-Host "Creating compressed release archive $ZipPath..."
Compress-Archive -Path "$AppDist\*" -DestinationPath $ZipPath -CompressionLevel Optimal
$SizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host "Built $ZipPath ($SizeMB MB)"
