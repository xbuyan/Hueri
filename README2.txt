HUERI patch 2: UNGM rate-limit safety + pagination
From ~/projects/Hueri with venv active:
  unzip -o ~/Downloads/hueri_patch2.zip -d .
  python apply_patch2.py          # does NOT contact UNGM; all-or-nothing; backup ungm.py.bak-patch2
  grep -n '^PAGE_SIZE_PER_TERM' app/collectors/ungm.py     # must say 15
Then WAIT until UNGM has cooled down (30+ min since the last 429) before the first
preview: python -m app.collectors.ungm
Rollback: cp app/collectors/ungm.py.bak-patch2 app/collectors/ungm.py
