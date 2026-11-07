import os
import subprocess
from typing import List, Optional

def ensure_kio_fuse_running() -> bool:
    """Ensure systemd user service kio-fuse is active."""
    try:
        subprocess.run(
            ["systemctl", "--user", "start", "kio-fuse.service"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        return True
    except Exception:
        return False

def resolve_network_url(url_or_path: str) -> str:
    """
    If given an smb://, sftp://, or other KIO URL, mount it via kio-fuse
    and return the real local POSIX path.
    """
    clean_str = url_or_path.strip().strip("'\"")
    if not clean_str:
        return clean_str

    # If it is already a valid local POSIX directory, return it as-is
    if os.path.isdir(clean_str):
        return clean_str

    # Check if it is a KIO network URL
    if clean_str.startswith(("smb://", "sftp://", "fish://", "ftp://", "nfs://")):
        ensure_kio_fuse_running()
        cmd = [
            "gdbus", "call", "--session",
            "--dest", "org.kde.KIOFuse",
            "--object-path", "/org/kde/KIOFuse",
            "--method", "org.kde.KIOFuse.VFS.mountUrl",
            clean_str
        ]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
            if p.returncode == 0:
                out = p.stdout.strip()
                # Parse gdbus response: ('/run/user/1000/kio-fuse-.../smb/...',)
                if out.startswith("('") and out.endswith("',)"):
                    local_path = out[2:-3]
                    if os.path.isdir(local_path):
                        return local_path
                elif out.startswith("(") and out.endswith(",)"):
                    local_path = out[1:-2].strip("'\"")
                    if os.path.isdir(local_path):
                        return local_path
        except Exception:
            pass

    return clean_str

def get_kio_fuse_smb_root() -> Optional[str]:
    """Find the active kio-fuse SMB root directory if available."""
    uid = os.getuid()
    base = f"/run/user/{uid}"
    if os.path.isdir(base):
        for entry in os.listdir(base):
            if entry.startswith("kio-fuse"):
                smb_dir = os.path.join(base, entry, "smb")
                if os.path.isdir(smb_dir):
                    return smb_dir
    return None
