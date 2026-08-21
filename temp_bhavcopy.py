"""Temp script: download yesterday's bhavcopy to data/ folder."""
from datetime import date, timedelta
from pathlib import Path

from jugaad_data.nse.archives import NSEArchives

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

target = date(2024, 7, 5)  # pre-UDIFF (UDIFF starts 2024-07-08)
print(f"Downloading bhavcopy for {target} into {DATA_DIR}")

archives = NSEArchives()
saved = archives.bhavcopy_save(target, str(DATA_DIR))
print(f"Saved: {saved}")
