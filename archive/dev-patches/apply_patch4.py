"""Patch 4: run the World Bank collector alongside UNGM in main.py.

Usage (from project root, venv active):
    python apply_patch4.py            # patches ./main.py, backup saved first
    python apply_patch4.py path.py    # patch a different file (for testing)

Each source runs independently: one failing (error, 429 cooldown) no longer
kills the run if the other succeeded. The existing 502 / ALLOW_MOCK_FALLBACK
behaviour only triggers when EVERY source failed.
"""
import shutil
import sys
from datetime import datetime

target = sys.argv[1] if len(sys.argv) > 1 else "main.py"
src = open(target, encoding="utf-8").read()

OLD_IMPORT = "from app.collectors.ungm import ungm_collector, UNGMCollectorError\n"
NEW_IMPORT = (
    "from app.collectors.ungm import ungm_collector, UNGMCollectorError\n"
    "from app.collectors.worldbank import worldbank_collector\n"
    "from app.collectors.base import CollectorError\n"
)

OLD_BLOCK = '''    try:
        # The collector is synchronous; keep it off the event loop.
        fetched_notices, unreviewed_notices = await asyncio.to_thread(ungm_collector.collect)
    except UNGMCollectorError as exc:
        logger.error("Live UNGM collection failed: %s", exc)
        if not settings.ALLOW_MOCK_FALLBACK:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Live tender collection failed and ALLOW_MOCK_FALLBACK is off: {exc}",
            )
        logger.warning("ALLOW_MOCK_FALLBACK is on — using labeled mock notices instead.")
        fetched_notices = ungm_collector.get_mock_data()
        is_mock_run = True
'''

NEW_BLOCK = '''    # Each source runs on its own: a failure (error or rate-limit cooldown) in
    # one is logged and skipped, and only if EVERY source fails does the
    # 502 / mock-fallback path below apply. External ids are source-prefixed
    # (UNGM-..., WB-...), so merging the lists cannot collide.
    collectors = [("UNGM", ungm_collector), ("World Bank", worldbank_collector)]
    fetched_notices = []
    unreviewed_notices = []
    collector_failures: list = []
    for source_name, collector in collectors:
        try:
            # Collectors are synchronous; keep them off the event loop.
            got, got_unreviewed = await asyncio.to_thread(collector.collect)
        except CollectorError as exc:
            logger.error("Live %s collection failed: %s", source_name, exc)
            collector_failures.append(f"{source_name}: {exc}")
            continue
        fetched_notices.extend(got)
        unreviewed_notices.extend(got_unreviewed)
    if collector_failures and len(collector_failures) < len(collectors):
        logger.warning(
            "Collected from %d of %d sources; failed: %s",
            len(collectors) - len(collector_failures), len(collectors),
            "; ".join(collector_failures),
        )
    if len(collector_failures) == len(collectors):
        exc = "; ".join(collector_failures)
        if not settings.ALLOW_MOCK_FALLBACK:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Live tender collection failed and ALLOW_MOCK_FALLBACK is off: {exc}",
            )
        logger.warning("ALLOW_MOCK_FALLBACK is on — using labeled mock notices instead.")
        fetched_notices = ungm_collector.get_mock_data()
        is_mock_run = True
'''

for label, old in (("import line", OLD_IMPORT), ("collection block", OLD_BLOCK)):
    count = src.count(old)
    if count != 1:
        sys.exit(f"ABORT: expected exactly 1 match for {label}, found {count}. Nothing changed.")

backup = f"{target}.bak-{datetime.now():%Y%m%d-%H%M%S}"
shutil.copy(target, backup)
src = src.replace(OLD_IMPORT, NEW_IMPORT).replace(OLD_BLOCK, NEW_BLOCK)
open(target, "w", encoding="utf-8").write(src)
compile(src, target, "exec")
print(f"Patched {target} (backup: {backup}). Syntax check passed.")
