"""Idempotent: wires demo_backend.web_mount into demo_backend/app.py (appends 2 lines)."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "demo_backend" / "app.py"
src = p.read_text()
if "mount_web" in src:
    print("already wired")
else:
    p.write_text(src.rstrip("\n") + "\n\nfrom demo_backend.web_mount import mount_web  # noqa: E402\nmount_web(app)\n")
    print("wired mount_web into", p)
