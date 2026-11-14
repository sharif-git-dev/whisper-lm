# WhisperLM

Private, GPU-accelerated Windows voice dictation, automatic meeting recording, multi-speaker separation, and local AI summarization powered by **whisper.cpp (Vulkan)**, **Silero VAD (ONNX)**, and **llama.cpp (Vulkan)**.

- **Website & Download:** https://sharif-git-dev.github.io/whisper-lm/
- **Latest Windows Release:** https://github.com/sharif-git-dev/whisper-lm/releases/latest

## Key Features
- **Push-to-Talk & Hands-Free (`Ctrl + Win`):** Hold `Ctrl + Win` anywhere on Windows to dictate immediately with a crisp activation pop cue, or double-tap `Ctrl + Win` to stay hands-free until tapped again.
- **Hover-Expandable Minimal Pill:** Compact obsidian bar (`42×22`) with our indigo-accented **WhisperLM** mark expands cleanly on hover to reveal a quick-record button for standalone saved recordings, and animates responsive waveforms while listening.
- **English Accuracy + Arabic On/Off Switch:** Runs `Whisper large-v3-turbo` with beam search (`--beam-size 5`), anti-aliased 16 kHz microphone conditioning, and a 1-click **Arabic + English** toggle in **Settings** and the system tray so pure English mode stays locked to English while bilingual mode supports mixed Arabic + English.
- **Automatic Meeting Detection & Dual-Stream Capture:** Detects Google Meet, Zoom, Microsoft Teams, Discord, WhatsApp, Webex, and Slack calls automatically, recording both your microphone and system audio via WASAPI loopback.
- **Multi-Speaker Attribution & Local Summaries:** Separates `You` vs remote speakers (`Speaker 1`, `Speaker 2`, ...) and summarizes full transcripts locally into structured **Overview**, **Key Points**, **Decisions**, and **Action Items** via `Qwen3-4B-Instruct-2507`.
- **100% Local History & Privacy:** Everything—audio capture, speech recognition, AI formatting, and time-organized archives (`Today`, `This Week`, `This Month`, `All`)—stays on your machine (`127.0.0.1`).
- **Launch at Windows Startup:** Starts silently in the system tray when you sign in to Windows (toggleable anytime from **Settings** or the tray menu).

## Quick Install

### Option 1: Portable Windows App
Download `WhisperLM-Windows-x64.zip` from the [Releases page](https://github.com/sharif-git-dev/whisper-lm/releases/latest), unzip anywhere, and run `WhisperLM.exe`. On first launch, WhisperLM opens automatically and downloads required AI models into `models/`.

### Option 2: Run from Source
```powershell
git clone https://github.com/sharif-git-dev/whisper-lm.git
cd whisper-lm
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
wscript start_silent.vbs
```

To build a portable release zip yourself:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```
