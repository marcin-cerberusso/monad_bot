"""
💾 BACKUP SYSTEM - Tworzenie kopii zapasowych kluczowych plików
"""
import shutil
import os
from datetime import datetime
from pathlib import Path

BACKUP_DIR = Path(__file__).parent / "backups"
FILES_TO_BACKUP = [
    ".env",
    "agents/positions.json",
    "agents/config.py",
    "positions.json"  # Check both locations
]

def create_backup():
    """Utwórz backup plików konfiguracyjnych"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / timestamp
    backup_path.mkdir(parents=True, exist_ok=True)
    
    print(f"📦 Creating backup in {backup_path}...")
    
    root_dir = Path(__file__).parent
    
    for filename in FILES_TO_BACKUP:
        src = root_dir / filename
        if src.exists():
            dst = backup_path / filename
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  ✅ Backed up: {filename}")
        else:
            # Try finding it relative to agents if not found in root
            if "agents/" not in filename:
                 src_alt = root_dir / "agents" / filename
                 if src_alt.exists():
                     dst = backup_path / filename
                     dst.parent.mkdir(parents=True, exist_ok=True)
                     shutil.copy2(src_alt, dst)
                     print(f"  ✅ Backed up: {filename} (from agents/)")

    # Cleanup old backups (keep last 10)
    backups = sorted(BACKUP_DIR.glob("*"))
    if len(backups) > 10:
        for old_backup in backups[:-10]:
            shutil.rmtree(old_backup)
            print(f"  🗑️ Removed old backup: {old_backup.name}")

if __name__ == "__main__":
    create_backup()
