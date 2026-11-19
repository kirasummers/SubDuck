import ctypes
import shutil
import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _dbus_inhibit(dest: str, obj_path: str, method: str, reason: str) -> int | None:
    if not shutil.which("gdbus"):
        return None
    cmd = [
        "gdbus", "call", "--session",
        "--dest", dest,
        "--object-path", obj_path,
        "--method", method,
        "SubDuck", reason
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
    if res.returncode == 0 and res.stdout.strip():
        tokens = res.stdout.strip().strip("(),").replace(",", " ").split()
        for tok in reversed(tokens):
            if tok.isdigit():
                return int(tok)
    return None


def _dbus_uninhibit(dest: str, obj_path: str, method: str, cookie: int):
    if not shutil.which("gdbus"):
        return
    cmd = [
        "gdbus", "call", "--session",
        "--dest", dest,
        "--object-path", obj_path,
        "--method", method,
        str(cookie)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)


class SleepInhibitor:
    def __init__(self):
        self._proc = None
        self._pm_cookie = None
        self._ss_cookie = None
        self._is_active = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    def acquire(self, reason: str = "Processing videos in SubDuck") -> bool:
        if self._is_active:
            return True

        if sys.platform == "win32":
            k32 = getattr(ctypes.windll, "kernel32", None)
            if k32:
                k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
            self._is_active = True
            return True

        if sys.platform == "darwin":
            if shutil.which("caffeinate"):
                self._proc = subprocess.Popen(
                    ["caffeinate", "-di"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            self._is_active = True
            return True

        if shutil.which("systemd-inhibit"):
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

        self._pm_cookie = _dbus_inhibit(
            "org.freedesktop.PowerManagement.Inhibit",
            "/org/freedesktop/PowerManagement/Inhibit",
            "org.freedesktop.PowerManagement.Inhibit.Inhibit",
            reason
        )
        self._ss_cookie = _dbus_inhibit(
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver.Inhibit",
            reason
        )

        self._is_active = True
        return True

    def release(self):
        if not self._is_active:
            return

        if sys.platform == "win32":
            k32 = getattr(ctypes.windll, "kernel32", None)
            if k32:
                k32.SetThreadExecutionState(ES_CONTINUOUS)
            self._is_active = False
            return

        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

        if self._pm_cookie is not None:
            _dbus_uninhibit(
                "org.freedesktop.PowerManagement.Inhibit",
                "/org/freedesktop/PowerManagement/Inhibit",
                "org.freedesktop.PowerManagement.Inhibit.UnInhibit",
                self._pm_cookie
            )
            self._pm_cookie = None

        if self._ss_cookie is not None:
            _dbus_uninhibit(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver.UnInhibit",
                self._ss_cookie
            )
            self._ss_cookie = None

        self._is_active = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
