param(
    [switch]$SkipModels
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

New-Item -ItemType Directory -Force -Path "bin\whisper", "bin\llama", "models", "logs" | Out-Null

if (-not (Test-Path "bin\whisper\Release\whisper-server.exe")) {
    Write-Host "Downloading whisper.cpp Vulkan Windows binaries..."
    New-Item -ItemType Directory -Force -Path "tmp_dl" | Out-Null
    Invoke-WebRequest -Uri "https://github.com/lemonade-sdk/whisper.cpp-builds/releases/download/v1.8.4/whisper-bin-x64-vulkan.zip" -OutFile "tmp_dl\whisper-vulkan.zip"
    Expand-Archive -Path "tmp_dl\whisper-vulkan.zip" -DestinationPath "bin\whisper" -Force
    Remove-Item -Recurse -Force "tmp_dl"
}

if (-not (Test-Path "bin\llama\llama-server.exe")) {
    Write-Host "Downloading llama.cpp Vulkan Windows binaries..."
    New-Item -ItemType Directory -Force -Path "tmp_dl" | Out-Null
    Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/download/b12374/llama-b12374-bin-win-vulkan-x64.zip" -OutFile "tmp_dl\llama-vulkan.zip"
    Expand-Archive -Path "tmp_dl\llama-vulkan.zip" -DestinationPath "bin\llama" -Force
    Remove-Item -Recurse -Force "tmp_dl"
}

if (-not (Test-Path "models\silero_vad.onnx")) {
    Write-Host "Downloading Silero VAD ONNX..."
    Invoke-WebRequest -Uri "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx" -OutFile "models\silero_vad.onnx"
}

if (-not $SkipModels) {
    if (-not (Test-Path "models\ggml-large-v3-turbo.bin") -or (Get-Item "models\ggml-large-v3-turbo.bin").Length -lt 1500000000) {
        Write-Host "Downloading Whisper large-v3-turbo GGML (~1.55 GB)..."
        curl.exe -L --retry 3 --progress-bar -o "models\ggml-large-v3-turbo.bin" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
    }
    if (-not (Test-Path "models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf") -or (Get-Item "models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf").Length -lt 2300000000) {
        Write-Host "Downloading Qwen3-4B-Instruct-2507-Q4_K_M.gguf (~2.38 GB)..."
        curl.exe -L --retry 3 --progress-bar -o "models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf" "https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
    }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating Python virtual environment..."
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install -e . --no-deps

Write-Host "Setup complete! Run: .\.venv\Scripts\python.exe -m whisper_lm"
