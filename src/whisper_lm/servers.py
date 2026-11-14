"""Subprocess lifecycle manager for whisper-server.exe and llama-server.exe."""

from __future__ import annotations

import atexit
import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import httpx

from whisper_lm.config import AppConfig

CREATE_NO_WINDOW = 0x08000000

# Win32 Job Object constants to guarantee child servers exit when the app closes.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_kill_on_close_job() -> int | None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            return None
        return int(job)
    except Exception:
        return None


def _assign_to_job(job_handle: int | None, proc: subprocess.Popen[bytes]) -> None:
    if not job_handle:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject(wintypes.HANDLE(job_handle), wintypes.HANDLE(int(proc._handle)))  # type: ignore[attr-defined]
    except Exception:
        pass


class ServerManager:
    """Manage local Vulkan whisper-server and llama-server background daemons."""

    def __init__(self, config: AppConfig, log_dir: Path | None = None) -> None:
        self.config = config
        self.log_dir = log_dir if log_dir is not None else config.resolve_path("logs")
        self._job_handle = _create_kill_on_close_job()
        self._whisper_proc: subprocess.Popen[bytes] | None = None
        self._llama_proc: subprocess.Popen[bytes] | None = None
        self._log_handles: list[object] = []
        atexit.register(self.stop)

    @property
    def whisper_base_url(self) -> str:
        return f"http://{self.config.whisper_host}:{self.config.whisper_port}"

    @property
    def llama_base_url(self) -> str:
        return f"http://{self.config.llama_host}:{self.config.llama_port}"

    @property
    def llama_running(self) -> bool:
        return self._llama_proc is not None and self._llama_proc.poll() is None

    def start(self, wait_timeout_s: float = 60.0) -> None:
        """Start configured local inference servers and wait until healthy."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._start_whisper()
        if self.config.llm_cleanup_enabled or self.config.meeting_cleanup_enabled or self.config.meeting_summary_enabled:
            llama_bin = self.config.resolve_path(self.config.llama_server_path)
            llama_model = self.config.resolve_path(self.config.llama_model_path)
            if llama_bin.exists() and llama_model.exists():
                self._start_llama()

        self._wait_until_ready(self.whisper_base_url, "/", self._whisper_proc, "whisper-server", wait_timeout_s)
        if self._llama_proc is not None:
            self._wait_until_ready(self.llama_base_url, "/health", self._llama_proc, "llama-server", wait_timeout_s)

    def ensure_llama_started(self, wait_timeout_s: float = 60.0) -> bool:
        """Start llama-server on demand if enabled later via UI settings."""
        if self.llama_running:
            return True
        llama_bin = self.config.resolve_path(self.config.llama_server_path)
        llama_model = self.config.resolve_path(self.config.llama_model_path)
        if not (llama_bin.exists() and llama_model.exists()):
            return False
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._start_llama()
        if self._llama_proc is not None:
            self._wait_until_ready(self.llama_base_url, "/health", self._llama_proc, "llama-server", wait_timeout_s)
            return True
        return False

    def _start_whisper(self) -> None:
        if self._whisper_proc is not None and self._whisper_proc.poll() is None:
            return
        exe = self.config.resolve_path(self.config.whisper_server_path)
        model = self.config.resolve_path(self.config.whisper_model_path)
        if not exe.exists():
            raise FileNotFoundError(f"whisper-server.exe not found: {exe}")
        if not model.exists():
            raise FileNotFoundError(f"Whisper model not found: {model}")

        cmd = [
            str(exe),
            "--model",
            str(model),
            "--host",
            self.config.whisper_host,
            "--port",
            str(self.config.whisper_port),
            "--device",
            str(self.config.whisper_device),
            "--language",
            "auto",
            "--beam-size",
            "5",
            "--best-of",
            "5",
            "--suppress-nst",
            "--flash-attn",
            "--no-timestamps",
        ]
        log_file = (self.log_dir / "whisper-server.log").open("ab")
        self._log_handles.append(log_file)
        self._whisper_proc = subprocess.Popen(
            cmd,
            cwd=str(exe.parent),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        _assign_to_job(self._job_handle, self._whisper_proc)

    def _start_llama(self) -> None:
        if self._llama_proc is not None and self._llama_proc.poll() is None:
            return
        exe = self.config.resolve_path(self.config.llama_server_path)
        model = self.config.resolve_path(self.config.llama_model_path)

        cmd = [
            str(exe),
            "--model",
            str(model),
            "--host",
            self.config.llama_host,
            "--port",
            str(self.config.llama_port),
            "--device",
            self.config.llama_device,
            "--gpu-layers",
            "all",
            "--ctx-size",
            str(self.config.llama_ctx_size),
            "--flash-attn",
            "auto",
            "--reasoning",
            "off",
        ]
        log_file = (self.log_dir / "llama-server.log").open("ab")
        self._log_handles.append(log_file)
        self._llama_proc = subprocess.Popen(
            cmd,
            cwd=str(exe.parent),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        _assign_to_job(self._job_handle, self._llama_proc)

    @staticmethod
    def _wait_until_ready(
        base_url: str,
        path: str,
        proc: subprocess.Popen[bytes] | None,
        name: str,
        timeout_s: float,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        with httpx.Client(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if proc is not None and proc.poll() is not None:
                    raise RuntimeError(f"{name} exited early with code {proc.returncode}. Check logs/{name}.log.")
                try:
                    resp = client.get(f"{base_url}{path}")
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
        raise TimeoutError(f"Timed out waiting for {name} at {base_url}{path}")

    def stop(self) -> None:
        """Terminate running inference servers cleanly."""
        for proc in (self._whisper_proc, self._llama_proc):
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._whisper_proc = None
        self._llama_proc = None

        for fh in self._log_handles:
            try:
                fh.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._log_handles.clear()
