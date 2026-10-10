#!/usr/bin/env python3
"""apply_patch12.py - three fixes found by the first local Docker run.

Run from the ROOT of the integrate-live-collectors worktree
(~/projects/Hueri-full), with patch 5 committed:

    python apply_patch12.py
    python -m pytest -q -p no:warnings 2>&1 | tail -3        # expect 74 passing

To undo:  git checkout -- docker-compose.prod.yml Dockerfile \
              frontend/src/components/Dashboard.jsx tests/test_deploy_files.py

FIX 1 - the worker would report "unhealthy".
  Every container built from the image inherits its HEALTHCHECK, which probes the
  api's HTTP port. The worker serves no HTTP, so after a couple of minutes Docker
  would mark it unhealthy (the `docker compose ps` output showed "health: starting").
  The worker's health check is now disabled; Docker's restart policy still brings
  it back if the process exits.

FIX 2 - the image build spent ~8 minutes installing a compiler nobody needs.
  The 17-minute build was dominated by `apt-get install build-essential` (497 s)
  and pip (363 s). Every pinned dependency publishes a prebuilt wheel for Python
  3.14 (the install log shows only wheels), so the compiler is removed. If a future
  pin ever needs one, pip fails loudly and the Dockerfile comment says what to add.

FIX 3 - the dashboard would call http://localhost:8000 when deployed.
  `VITE_API_BASE_URL || "http://localhost:8000"` falls back to localhost when the
  variable is unset OR empty, and the committed frontend/dist bundle was built that
  way, so a browser on the client's domain would try to reach its own machine.
  The default is now: same origin in a production build (nginx proxies /api), the
  local api only under `npm run dev`. One built bundle then works for every client
  deployment. You must rebuild the bundle after this patch:

      cd frontend && npm ci && npm run build && cd ..

Also adds 2 guard tests to tests/test_deploy_files.py. Every change is validated
before anything is written; any failure rolls everything back. Safe to re-run.
"""
import pathlib
import py_compile
import re
import subprocess
import sys

ROOT = pathlib.Path.cwd()
EXPECTED_BRANCH = "integrate-live-collectors"

COMPOSE = ROOT / "docker-compose.prod.yml"
DOCKERFILE = ROOT / "Dockerfile"
DASHBOARD = ROOT / "frontend" / "src" / "components" / "Dashboard.jsx"
TEST_DEPLOY = ROOT / "tests" / "test_deploy_files.py"

COMPOSE_OLD = "    command: arq app.worker.WorkerSettings\n"
COMPOSE_NEW = (
    COMPOSE_OLD
    + "    # The image's HEALTHCHECK probes the api's HTTP port, which the worker does not\n"
    + "    # serve; without this Docker would mark the worker unhealthy.\n"
    + "    healthcheck:\n"
    + "      disable: true\n"
)

DOCKER_OLD = (
    "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
    "    build-essential \\\n"
    "    && rm -rf /var/lib/apt/lists/*\n"
    "\n"
)
DOCKER_NEW = (
    "# No compiler is installed: every pinned dependency publishes a prebuilt wheel for\n"
    "# this Python, so the build stays fast. If a future pin needs to compile, pip will\n"
    "# fail with a clear error; add build-essential here (apt-get update && apt-get\n"
    "# install -y --no-install-recommends build-essential) in that case.\n"
    "\n"
)

DASHBOARD_RE = re.compile(
    r"(?:^// Backend base URL[^\n]*\n(?://[^\n]*\n)?)?"
    r'^const API_BASE_URL = import\.meta\.env\?\.VITE_API_BASE_URL \|\| "http://localhost:8000";\n',
    re.M,
)
DASHBOARD_NEW = """// Backend base URL. A production build is served by the same nginx that proxies
// /api, so the default there is the same origin (empty = relative URLs) and one
// build works for every deployment. `npm run dev` talks to the local api; set
// VITE_API_BASE_URL (even to an empty string) to override either default.
const API_BASE_URL =
  import.meta.env?.VITE_API_BASE_URL ?? (import.meta.env?.DEV ? "http://localhost:8000" : "");
"""

TESTS_APPENDED = '''

def test_worker_does_not_inherit_the_api_healthcheck():
    services = yaml.safe_load(_read("docker-compose.prod.yml"))["services"]
    assert services["worker"]["healthcheck"] == {"disable": True}


def test_frontend_calls_the_api_on_its_own_origin_in_production():
    source = _read("frontend/src/components/Dashboard.jsx")
    assert '|| "http://localhost:8000"' not in source
    assert "import.meta.env?.DEV" in source
'''


def fail(msg):
    print(f"ABORTED: {msg}\nNothing was changed.")
    sys.exit(1)


def git(*args):
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        fail(f"git {' '.join(args)} failed: {exc.stderr.strip()}")


def read(path):
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} not found - run this from ~/projects/Hueri-full")
    return path.read_text()


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        fail(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new)


def main():
    dashboard = read(DASHBOARD)
    if "import.meta.env?.DEV" in dashboard:
        print("Dashboard.jsx already has the same-origin default - patch 12 appears to be applied.")
        return

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != EXPECTED_BRANCH:
        fail(f"you are on branch '{branch}', expected '{EXPECTED_BRANCH}'")
    paths = [str(p.relative_to(ROOT)) for p in (COMPOSE, DOCKERFILE, DASHBOARD, TEST_DEPLOY)]
    dirty = git("status", "--porcelain", "--", *paths).stdout.strip()
    if dirty:
        fail("these files have uncommitted changes (commit patch 5 first):\n" + dirty)

    compose = replace_once(read(COMPOSE), COMPOSE_OLD, COMPOSE_NEW, "docker-compose.prod.yml worker command")
    dockerfile = replace_once(read(DOCKERFILE), DOCKER_OLD, DOCKER_NEW, "Dockerfile build-essential step")

    matches = list(DASHBOARD_RE.finditer(dashboard))
    if len(matches) != 1:
        fail(f"Dashboard.jsx: expected exactly one API_BASE_URL definition, found {len(matches)}")
    m = matches[0]
    dashboard_new = dashboard[: m.start()] + DASHBOARD_NEW + dashboard[m.end():]

    tests = read(TEST_DEPLOY)
    if "test_worker_does_not_inherit_the_api_healthcheck" in tests:
        fail("tests/test_deploy_files.py already has the worker healthcheck test; resolve by hand")
    tests_new = tests.rstrip("\n") + "\n" + TESTS_APPENDED

    planned = {
        COMPOSE: compose,
        DOCKERFILE: dockerfile,
        DASHBOARD: dashboard_new,
        TEST_DEPLOY: tests_new,
    }
    originals = {}
    try:
        for path, text in planned.items():
            originals[path] = path.read_text()
            path.write_text(text)
            print(f"wrote {path.relative_to(ROOT)}")
        py_compile.compile(str(TEST_DEPLOY), doraise=True)
    except Exception as exc:  # roll back on any failure
        print(f"ERROR: {exc}\nRolling back...")
        for path, old in originals.items():
            path.write_text(old)
        print("Rolled back. Nothing was changed.")
        sys.exit(1)

    print("\nDone. Next:")
    print("  python -m pytest -q -p no:warnings 2>&1 | tail -3     # expect 74 passing")
    print("  cd frontend && npm ci && npm run build && cd ..        # rebuild the bundle (tracked in git)")


if __name__ == "__main__":
    main()
