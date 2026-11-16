"""
backup_db.py — Safe online backup of the SQLite database (works while the bot is running).

    python scripts/backup_db.py              # → backups/travel_bot-YYYYmmdd-HHMMSS.db, keeps the newest 14
    python scripts/backup_db.py --keep 30 --dir /data/backups

Schedule it daily (cron:  0 3 * * *  cd /opt/travel-buddy && venv/bin/python scripts/backup_db.py).
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def main() -> int:
    db_path = Path(os.getenv("DATABASE_PATH") or ROOT / "travel_bot.db")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(db_path.parent / "backups"))
    ap.add_argument("--keep", type=int, default=14, help="number of backups to keep")
    args = ap.parse_args()

    if not db_path.exists():
        print(f"No database at {db_path}")
        return 1
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{db_path.stem}-{datetime.now():%Y%m%d-%H%M%S}.db"

    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)  # consistent snapshot, includes WAL contents
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        dst.close()
        src.close()
    if not ok:
        target.unlink(missing_ok=True)
        print("Backup failed the integrity check; nothing kept.")
        return 1

    backups = sorted(out_dir.glob(f"{db_path.stem}-*.db"))
    for old in backups[:-args.keep] if args.keep > 0 else []:
        old.unlink()
    print(f"Backup written: {target} ({target.stat().st_size // 1024} KB); keeping {min(len(backups), args.keep)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
