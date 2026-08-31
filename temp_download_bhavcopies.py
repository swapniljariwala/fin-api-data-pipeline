"""Temp script: download daily NSE CM bhavcopies from 2010 to today into data/.

Skips files already present on disk. Holidays/weekends resolve to no file and
are skipped without error. Run with the venv active:

    python temp_download_bhavcopies.py
"""
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests
from jugaad_data.nse.archives import NSEArchives

START = date(2010, 1, 1)
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

archives = NSEArchives()

downloaded = skipped = skipped_holiday = failed = 0
errors = []

d = START
today = date.today()
while d <= today:
    fname = DATA_DIR / d.strftime("cm%d%b%Ybhav.csv")
    if fname.exists() and fname.stat().st_size > 0:
        skipped += 1
        d += timedelta(days=1)
        continue

    if d.weekday() >= 5:  # weekend
        d += timedelta(days=1)
        continue

    try:
        saved = archives.bhavcopy_save(d, str(DATA_DIR))
        time.sleep(2)
        if saved:
            downloaded += 1
            print(f"OK {d} -> {Path(saved).name}")
        else:
            skipped_holiday += 1
    except (requests.RequestException, ValueError, zipfile.BadZipFile) as e:
        # No bhavcopy issued for this date (holiday or data gap); not fatal.
        skipped_holiday += 1
        if d.weekday() < 5:
            print(f"-- {d} (no bhavcopy): {type(e).__name__}")
    except Exception as e:
        failed += 1
        errors.append((d, repr(e)))
        print(f"!! {d}: {e!r}")
    d += timedelta(days=1)

print("\nDone.")
print(f"downloaded={downloaded} skipped(already present)={skipped} "
      f"skipped(no bhavcopy)={skipped_holiday} failed={failed}")
if errors:
    print("Failures:")
    for day, err in errors:
        print(f"  {day}: {err}")