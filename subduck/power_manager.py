import atexit
import ctypes
import os
import shutil
import signal
import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_active_inhibitors = set()


def _set_pdeathsig():
    """Ensure child process receives SIGTERM when parent process exits unexpectedly."""
    try:
        libc = ctypes.CDLL("libc.so.6")
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


def _kill_stale_subduck_inhibitors():
    """Find and terminate any orphaned systemd-inhibit processes created by SubDuck from prior crashes."""
    if sys.platform != "linux":
        return
    current_pid = os.getpid()
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == current_pid:
                continue
            try:
                cmdline_path = os.path.join(entry.path, "cmdline")
                with open(cmdline_path, "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="ignore").replace("\x00", " ")
                if "systemd-inhibit" in cmdline and "--who=SubDuck" in cmdline:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
            except (FileNotFoundError, PermissionError):
                continue
    except Exception:
        pass


def _cleanup_all_active_inhibitors():
    for inhibitor in list(_active_inhibitors):
        try:
            inhibitor.release()
        except Exception:
            pass


atexit.register(_cleanup_all_active_inhibitors)


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
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
        if res.returncode == 0 and res.stdout.strip():
            tokens = res.stdout.strip().strip("(),").replace(",", " ").split()
            for tok in reversed(tokens):
                if tok.isdigit():
                    return int(tok)
    except Exception:
        pass
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
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass


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

        _kill_stale_subduck_inhibitors()

        if sys.platform == "win32":
            k32 = getattr(ctypes.windll, "kernel32", None)
            if k32:
                k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
            self._is_active = True
            _active_inhibitors.add(self)
            return True

        if sys.platform == "darwin":
            if shutil.which("caffeinate"):
                self._proc = subprocess.Popen(
                    ["caffeinate", "-di", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            self._is_active = True
            _active_inhibitors.add(self)
            return True

        if shutil.which("systemd-inhibit"):
            cat_bin = shutil.which("cat") or "cat"
            self._proc = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=idle:sleep",
                    "--who=SubDuck",
                    f"--why={reason}",
                    cat_bin
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=_set_pdeathsig if sys.platform.startswith("linux") else None
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
        _active_inhibitors.add(self)
        return True

    def release(self):
        if not self._is_active:
            return

        _active_inhibitors.discard(self)

        if sys.platform == "win32":
            k32 = getattr(ctypes.windll, "kernel32", None)
            if k32:
                k32.SetThreadExecutionState(ES_CONTINUOUS)
            self._is_active = False
            return

        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
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
