HUERI: unreviewed bucket + alerted_at patch
===========================================
From ~/projects/Hueri, venv active:

  1. unzip this folder's files into the project root (apply_patch.py and
     migrate_add_columns.py must sit next to main.py)
  2. python apply_patch.py            # all-or-nothing; backs up as *.bak-unreviewed
  3. python migrate_add_columns.py    # adds the 2 columns; safe to re-run
  4. python -m app.collectors.ungm    # preview candidates + unreviewed titles (no DB, no Gemini)
  5. restart uvicorn, then POST /api/tenders/trigger-collect
     -> new header X-Unreviewed = number of newly saved unreviewed tenders

Rollback: copy the *.bak-unreviewed files back over the originals.
