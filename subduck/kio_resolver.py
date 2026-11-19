import os
import shutil
import subprocess


def ensure_kio_fuse_running() -> bool:
    if not shutil.which("systemctl"):
        return False
    res = subprocess.run(
        ["systemctl", "--user", "start", "kio-fuse.service"],
        capture_output=True,
        timeout=5
    )
    return res.returncode == 0


def resolve_network_url(url_or_path: str) -> str:
    clean_str = url_or_path.strip().strip("'\"")
    if not clean_str or os.path.exists(clean_str):
        return clean_str

    if clean_str.startswith(("smb://", "sftp://", "fish://", "ftp://", "nfs://")) and shutil.which("gdbus"):
        ensure_kio_fuse_running()
        cmd = [
            "gdbus", "call", "--session",
            "--dest", "org.kde.KIOFuse",
            "--object-path", "/org/kde/KIOFuse",
            "--method", "org.kde.KIOFuse.VFS.mountUrl",
            clean_str
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if res.returncode == 0:
            out = res.stdout.strip()
            if out.startswith("(") and out.endswith(",)"):
                local_path = out[1:-2].strip("'\"")
                if os.path.exists(local_path):
                    return local_path

    return clean_str


def get_kio_fuse_smb_root() -> str | None:
    if not hasattr(os, "getuid"):
        return None
    base = f"/run/user/{os.getuid()}"
    if not os.path.isdir(base):
        return None
    for entry in os.listdir(base):
        if entry.startswith("kio-fuse"):
            smb_dir = os.path.join(base, entry, "smb")
            if os.path.isdir(smb_dir):
                return smb_dir
    return None
