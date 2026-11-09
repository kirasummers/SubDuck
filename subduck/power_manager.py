"""Sleep and idle inhibitor for keeping the system awake during processing."""
import os
import signal
import subprocess
from typing import Optional

class SleepInhibitor:
    """Inhibits system sleep, suspend, and screensaver during long operations."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._pm_cookie: Optional[int] = None
        self._ss_cookie: Optional[int] = None
        self._is_active: bool = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    def acquire(self, reason: str = "Processing videos and audio in SubDuck") -> bool:
        """Acquire sleep and idle inhibition lock."""
        if self._is_active:
            return True

        # systemd-inhibit process
        try:
            self._proc = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=idle:sleep",
                    "--who=SubDuck",
                    f"--why={reason}",
                    "sleep", "infinity"
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            self._proc = None

        # FreeDesktop PowerManagement DBus
        try:
            cmd = [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.PowerManagement.Inhibit",
                "--object-path", "/org/freedesktop/PowerManagement/Inhibit",
                "--method", "org.freedesktop.PowerManagement.Inhibit.Inhibit",
                "SubDuck", reason
            ]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
            if p.returncode == 0 and p.stdout.strip():
                out = p.stdout.strip()
                tokens = out.replace("(", "").replace(")", "").replace(",", "").split()
                if len(tokens) >= 2 and tokens[1].isdigit():
                    self._pm_cookie = int(tokens[1])
                elif tokens and tokens[0].isdigit():
                    self._pm_cookie = int(tokens[0])
        except Exception:
            self._pm_cookie = None

        # FreeDesktop ScreenSaver DBus
        try:
            cmd = [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.ScreenSaver",
                "--object-path", "/org/freedesktop/ScreenSaver",
                "--method", "org.freedesktop.ScreenSaver.Inhibit",
                "SubDuck", reason
            ]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
            if p.returncode == 0 and p.stdout.strip():
                out = p.stdout.strip()
                tokens = out.replace("(", "").replace(")", "").replace(",", "").split()
                if len(tokens) >= 2 and tokens[1].isdigit():
                    self._ss_cookie = int(tokens[1])
                elif tokens and tokens[0].isdigit():
                    self._ss_cookie = int(tokens[0])
        except Exception:
            self._ss_cookie = None

        self._is_active = True
        return True

    def release(self):
        """Release all active sleep and idle locks."""
        if not self._is_active:
            return

        # Terminate systemd-inhibit
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        # Release PowerManagement cookie
        if self._pm_cookie is not None:
            try:
                cmd = [
                    "gdbus", "call", "--session",
                    "--dest", "org.freedesktop.PowerManagement.Inhibit",
                    "--object-path", "/org/freedesktop/PowerManagement/Inhibit",
                    "--method", "org.freedesktop.PowerManagement.Inhibit.UnInhibit",
                    str(self._pm_cookie)
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except Exception:
                pass
            self._pm_cookie = None

        # Release ScreenSaver cookie
        if self._ss_cookie is not None:
            try:
                cmd = [
                    "gdbus", "call", "--session",
                    "--dest", "org.freedesktop.ScreenSaver",
                    "--object-path", "/org/freedesktop/ScreenSaver",
                    "--method", "org.freedesktop.ScreenSaver.UnInhibit",
                    str(self._ss_cookie)
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except Exception:
                pass
            self._ss_cookie = None

        self._is_active = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
