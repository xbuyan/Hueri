#!/usr/bin/env python3
"""Local demo environment for the read-only Hueri MCP service.

Run from the repository root with the project's interpreter:

    .venv/bin/python scripts/mcp_demo.py up       # throwaway Postgres on 127.0.0.1:55432 + SELECT-only role
    .venv/bin/python scripts/mcp_demo.py seed     # sample data (flagged is_mock) or --from-sqlite PATH
    .venv/bin/python scripts/mcp_demo.py verify   # prove the role cannot write, read users, or create tables
    .venv/bin/python scripts/mcp_demo.py run      # serve MCP on 127.0.0.1:8765 with two workers
    .venv/bin/python scripts/mcp_demo.py check    # end-to-end protocol, auth and worker checks (second terminal)
    .venv/bin/python scripts/mcp_demo.py down     # remove the container (add --purge to delete the env files)

Secrets live outside the repository, in ~/.config/hueri-mcp/ (mode 0600):
  demo.env        what the server reads (bearer token, read-only database URL, allowlists)
  demo-admin.env  throwaway admin password, used only by this script
Never touches any other database or container.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "hueri-mcp-demo-pg"
PG_PORT = 55432
DB_NAME = "hueri_demo"
RO_ROLE = "mcp_ro"
PROBE_ROLE = "mcp_seatbelt_probe"
MCP_PORT = 8765
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hueri-mcp"
SERVER_ENV = CONFIG_DIR / "demo.env"
ADMIN_ENV = CONFIG_DIR / "demo-admin.env"


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------- env files

def write_env(path: Path, values: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    os.chmod(path, 0o600)


def read_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# ---------------------------------------------------------------- docker / ports

def docker(*args: str, env: Optional[Dict[str, str]] = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, env=env)
    except FileNotFoundError:
        die("docker is not installed or not on PATH")
    except subprocess.CalledProcessError as exc:
        die(f"docker {' '.join(args[:2])} failed: {exc.stderr.strip()[:300]}")


def container_state() -> Optional[str]:
    result = docker("inspect", "-f", "{{.State.Running}}", CONTAINER, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_ready(timeout: int = 60) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        result = docker("exec", CONTAINER, "pg_isready", "-U", "postgres", "-d", DB_NAME, check=False)
        if result.returncode == 0:
            time.sleep(1)  # pg_isready can answer during the entrypoint's temporary server
            if docker("exec", CONTAINER, "pg_isready", "-U", "postgres", "-d", DB_NAME, check=False).returncode == 0:
                return
        time.sleep(1)
    die("Postgres did not become ready in time")


def admin_url() -> str:
    password = read_env(ADMIN_ENV).get("MCP_DEMO_ADMIN_PASSWORD")
    if not password:
        die(f"{ADMIN_ENV} is missing; run 'down' then 'up'")
    return f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{PG_PORT}/{DB_NAME}"


def ro_url(password: str) -> str:
    return f"postgresql+asyncpg://{RO_ROLE}:{password}@127.0.0.1:{PG_PORT}/{DB_NAME}"


def grant_statements() -> List[str]:
    return [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RO_ROLE}",
        f"REVOKE CREATE ON SCHEMA public FROM {RO_ROLE}",
        f"GRANT CONNECT ON DATABASE {DB_NAME} TO {RO_ROLE}",
        f"GRANT USAGE ON SCHEMA public TO {RO_ROLE}",
        f"GRANT SELECT ON tenders, tender_analyses TO {RO_ROLE}",
        f"ALTER ROLE {RO_ROLE} SET default_transaction_read_only = on",
    ]


# ---------------------------------------------------------------- sample and copied data

def _t(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, 0, 0)


def sample_rows() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Clearly-labelled synthetic data. Every row is_mock=True; titles start with [SAMPLE]."""
    def tender(i, source, title, buyer, deadline, summary, unreviewed=False):
        return dict(
            id=i, external_id=f"SAMPLE-{i:03d}", source=source, title=f"[SAMPLE] {title}",
            buyer=buyer, url=f"https://example.org/sample/{i}", deadline_str=deadline,
            raw_summary=summary, is_mock=True, is_unreviewed=unreviewed, created_at=_t(10 + i % 8, 8 + i % 9),
        )

    def analysis(i, score, fit, summary, services, gaps, steps, status="NEW"):
        return dict(
            tender_id=i, relevance_score=score, is_fit=fit, executive_summary=summary,
            matched_services=services, eligibility_gaps=gaps, suggested_next_steps=steps,
            status=status, evaluated_at=_t(10 + i % 8, 12),
        )

    tenders = [
        tender(1, "ungm", "ESIA for coastal road rehabilitation", "Sample Roads Authority",
               "30-Oct-2026 10:00 (GMT +3.00)", "Environmental and social impact assessment for a 40 km coastal road rehabilitation."),
        tender(2, "worldbank", "Environmental audit of an industrial park", "Sample Industrial Board",
               "2026-11-05T17:00:00+03:00", "Independent environmental audit covering air, water and waste compliance."),
        tender(3, "afdb", "Solid waste management feasibility study", "Sample Municipal Council",
               "12-Nov-2026", "Feasibility study for an integrated solid waste facility."),
        tender(4, "ungm", "Water treatment plant design supervision", "Sample Water Utility",
               "20-Nov-2026 12:00 (GMT +3.00)", "Design review and construction supervision for a treatment plant."),
        tender(5, "ppip", "Supply of office furniture", "Sample Ministry Procurement",
               "28-Oct-2026", "Supply and delivery of desks and chairs."),
        tender(6, "worldbank", "Climate resilience assessment for smallholder farms", "Sample Agriculture Programme",
               "15-Nov-2026", "Assess climate risks and adaptation options across three counties."),
        tender(7, "ungm", "Social impact baseline study", "Sample Relief Agency",
               "08-Nov-2026 09:30 (GMT +3.00)", "Baseline social impact study for a livelihoods programme."),
        tender(8, "ppip", "Supply of stationery", "Sample School District", "N/A",
               "Annual stationery supply contract.", unreviewed=True),
        tender(9, "ppip", "Laptop procurement", "Sample Training College", "soon",
               "Procurement of 50 laptops.", unreviewed=True),
        tender(10, "afdb", "Biodiversity survey of a protected wetland", "Sample Wildlife Service",
               "25-Nov-2026", "Field biodiversity survey and monitoring plan."),
        tender(11, "ungm", "Hazardous waste audit", "Sample Port Authority", "N/A",
               "Audit of hazardous waste handling at port facilities."),
        tender(12, "worldbank", "Wetland restoration consultancy", None,
               "2026-11-12T17:00:00+03:00", "Consultancy to design and supervise wetland restoration."),
    ]
    analyses = [
        analysis(1, 9.2, True, "Strong match: core ESIA work in the East Africa region.",
                 ["Environmental and Social Impact Assessment (ESIA)", "Environmental Management Plan"],
                 [], ["Download the terms of reference", "Confirm lead expert availability"]),
        analysis(2, 8.4, True, "Good match: environmental audit is a core service.",
                 ["Environmental Audit"], ["Requires an active NEMA firm licence"],
                 ["Check licence status", "Prepare compliance references"]),
        analysis(3, 7.1, True, "Reasonable match; waste management is adjacent to core services.",
                 ["Environmental Audit"], ["Needs a municipal solid waste reference project"],
                 ["Identify a waste management partner"], "UNDER_REVIEW"),
        analysis(4, 5.2, False, "Weak match: engineering design is outside the service profile.",
                 [], ["Requires a licensed civil engineering firm"], ["Consider declining"]),
        analysis(5, 1.8, False, "Not relevant: goods supply.", [], ["Supplier registration required"], [], "DISCARDED"),
        analysis(6, 8.8, True, "Strong match: climate and social assessment work.",
                 ["Climate Risk Assessment", "Social Impact Assessment"], [],
                 ["Request the full RFP", "Draft a methodology outline"], "BIDDING"),
        analysis(7, 7.6, True, "Good match: social baseline studies are a listed service.",
                 ["Social Impact Assessment"], ["Requires a regional survey team"],
                 ["Confirm field team availability"]),
        analysis(11, 6.4, False, "Partial match; hazardous waste auditing needs specialist accreditation.",
                 ["Environmental Audit"], ["Requires hazardous waste accreditation"], ["Seek an accredited partner"]),
        analysis(12, 9.0, True, "Strong match: restoration design and supervision.",
                 ["Ecosystem Restoration Planning", "Environmental Management Plan"], [],
                 ["Prepare the technical proposal", "Line up wetland ecology expertise"]),
    ]
    return tenders, analyses


_TENDER_COLUMNS = ("id", "external_id", "source", "title", "buyer", "url", "deadline_str",
                   "raw_summary", "is_mock", "is_unreviewed", "created_at")
_ANALYSIS_COLUMNS = ("id", "tender_id", "relevance_score", "is_fit", "executive_summary",
                     "matched_services", "eligibility_gaps", "suggested_next_steps", "status", "evaluated_at")


def _when(value: Any) -> Optional[dt.datetime]:
    if value is None or isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def read_sqlite_rows(path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Copy tenders and tender_analyses only, read-only. Never touches users or other tables."""
    import sqlite3

    if not Path(path).is_file():
        die(f"no such file: {path}")
    con = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tenders = [dict(r) for r in con.execute(f"SELECT {', '.join(_TENDER_COLUMNS)} FROM tenders ORDER BY id")]
        analyses = [dict(r) for r in con.execute(f"SELECT {', '.join(_ANALYSIS_COLUMNS)} FROM tender_analyses ORDER BY id")]
    except sqlite3.Error as exc:
        die(f"could not read tenders/tender_analyses from {path}: {exc}")
    finally:
        con.close()
    for row in tenders:
        row["is_mock"], row["is_unreviewed"] = bool(row["is_mock"]), bool(row["is_unreviewed"])
        row["created_at"] = _when(row["created_at"])
    for row in analyses:
        row["is_fit"] = bool(row["is_fit"])
        for key in ("matched_services", "eligibility_gaps", "suggested_next_steps"):
            row[key] = _json(row[key])
        row["status"] = None if row["status"] is None else str(row["status"])
        row["evaluated_at"] = _when(row["evaluated_at"])
    return tenders, analyses


def build_objects(tenders: List[Dict[str, Any]], analyses: List[Dict[str, Any]]):
    from sqlalchemy import null

    from app.models import Tender, TenderAnalysis, TenderStatus

    def status_of(name: Optional[str]):
        if name is None:
            return null()
        for lookup in (lambda: TenderStatus[name], lambda: TenderStatus(name)):
            try:
                return lookup()
            except (KeyError, ValueError):
                continue
        return null()

    tender_objs = [Tender(**row) for row in tenders]
    analysis_objs = []
    for row in analyses:
        data = dict(row)
        data["status"] = status_of(data.get("status"))
        analysis_objs.append(TenderAnalysis(**data))
    return tender_objs, analysis_objs


# ---------------------------------------------------------------- commands

async def _provision(ro_password: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models import Base

    engine = create_async_engine(admin_url())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            exists = (await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": RO_ROLE})).scalar()
            await conn.execute(text(f"{'ALTER' if exists else 'CREATE'} ROLE {RO_ROLE} LOGIN PASSWORD '{ro_password}'"))
            for statement in grant_statements():
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


def cmd_up(args) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    state = container_state()
    if state is None:
        if port_in_use(PG_PORT):
            die(f"port {PG_PORT} is already in use by something else; this script will not touch it")
        admin_password = secrets.token_urlsafe(24)
        write_env(ADMIN_ENV, {"MCP_DEMO_ADMIN_PASSWORD": admin_password})
        env = {**os.environ, "POSTGRES_PASSWORD": admin_password}
        docker("run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{PG_PORT}:5432",
               "-e", "POSTGRES_PASSWORD", "-e", f"POSTGRES_DB={DB_NAME}", "postgres:16-alpine", env=env)
        print(f"started container {CONTAINER} on 127.0.0.1:{PG_PORT}")
    elif state == "false":
        docker("start", CONTAINER)
        print(f"restarted container {CONTAINER}")
    else:
        print(f"container {CONTAINER} already running")
    wait_ready()
    ro_password = secrets.token_urlsafe(24)
    asyncio.run(_provision(ro_password))
    existing = read_env(SERVER_ENV)
    write_env(SERVER_ENV, {
        "MCP_BEARER_TOKEN": existing.get("MCP_BEARER_TOKEN") or secrets.token_urlsafe(32),
        "MCP_DATABASE_URL": ro_url(ro_password),
        "MCP_ALLOWED_HOSTS": f"127.0.0.1:{MCP_PORT},localhost:{MCP_PORT}",
        "MCP_ALLOWED_ORIGINS": "",
        "MCP_PORT": str(MCP_PORT),
    })
    print(f"read-only role '{RO_ROLE}' ready; server settings written to {SERVER_ENV} (mode 0600)")
    print("next: seed")


async def _seed(tenders, analyses, replace: bool) -> None:
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.models import Tender

    engine = create_async_engine(admin_url())
    try:
        async with AsyncSession(engine) as session:
            existing = (await session.execute(select(func.count()).select_from(Tender))).scalar()
            if existing and not replace:
                die(f"{existing} tenders already present; use --replace to wipe and reseed the demo database")
            if replace:
                await session.execute(text("DELETE FROM tender_analyses"))
                await session.execute(text("DELETE FROM tenders"))
            tender_objs, analysis_objs = build_objects(tenders, analyses)
            session.add_all(tender_objs)
            await session.flush()
            session.add_all(analysis_objs)
            await session.flush()
            for table in ("tenders", "tender_analyses"):
                await session.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"GREATEST((SELECT COALESCE(MAX(id), 1) FROM {table}), 1))"))
            await session.commit()
    finally:
        await engine.dispose()


def cmd_seed(args) -> None:
    if args.from_sqlite:
        tenders, analyses = read_sqlite_rows(args.from_sqlite)
        origin = f"copied from {args.from_sqlite} (tenders and tender_analyses only)"
    else:
        tenders, analyses = sample_rows()
        origin = "synthetic sample data, every row flagged is_mock (search with include_mock=true)"
    asyncio.run(_seed(tenders, analyses, args.replace))
    mocks = sum(1 for t in tenders if t["is_mock"])
    print(f"seeded {len(tenders)} tenders and {len(analyses)} analyses: {origin}; {mocks} flagged is_mock")


Row = Tuple[str, str, str, str]  # label, expected, got, verdict


async def _verify() -> List[Row]:
    import asyncpg
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.mcp_server import tools
    from app.mcp_server.db import make_engine, make_session_factory

    env = read_env(SERVER_ENV)
    url = env.get("MCP_DATABASE_URL")
    if not url:
        die(f"{SERVER_ENV} is missing; run 'up' first")
    dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    rows: List[Row] = []

    def record(label: str, expected: str, got: str) -> None:
        rows.append((label, expected, got, "PASS" if got == expected else "FAIL"))

    async def attempt(label: str, sql: str, expected: str, *, seatbelt_off: bool = False) -> None:
        settings = {"default_transaction_read_only": "off"} if seatbelt_off else None
        conn = await asyncpg.connect(dsn, server_settings=settings)
        try:
            try:
                await conn.execute(sql)
                got = "ok"
            except asyncpg.PostgresError as exc:
                got = exc.sqlstate
        finally:
            await conn.close()
        record(label, expected, got)

    # 1. Privileges alone (session seatbelt switched off at connect so only grants decide)
    p = {"seatbelt_off": True}
    await attempt("privileges: SELECT tenders", "SELECT 1 FROM tenders LIMIT 1", "ok", **p)
    await attempt("privileges: SELECT tender_analyses", "SELECT 1 FROM tender_analyses LIMIT 1", "ok", **p)
    for table in ("users", "notifications", "company_profiles"):
        await attempt(f"privileges: SELECT {table} denied", f"SELECT 1 FROM {table} LIMIT 1", "42501", **p)
    insert = "INSERT INTO tenders (external_id, source, title) VALUES ('verify', 'verify', 'verify')"
    await attempt("privileges: INSERT tenders denied", insert, "42501", **p)
    await attempt("privileges: UPDATE tenders denied", "UPDATE tenders SET title = title WHERE false", "42501", **p)
    await attempt("privileges: DELETE tenders denied", "DELETE FROM tenders WHERE false", "42501", **p)
    await attempt("privileges: INSERT tender_analyses denied",
                  "INSERT INTO tender_analyses (tender_id) VALUES (1)", "42501", **p)
    await attempt("privileges: UPDATE tender_analyses denied",
                  "UPDATE tender_analyses SET is_fit = is_fit WHERE false", "42501", **p)
    await attempt("privileges: DELETE tender_analyses denied", "DELETE FROM tender_analyses WHERE false", "42501", **p)
    await attempt("privileges: INSERT notifications denied", "INSERT INTO notifications DEFAULT VALUES", "42501", **p)
    await attempt("privileges: CREATE TABLE denied", "CREATE TABLE verify_probe (id int)", "42501", **p)
    await attempt("privileges: self-GRANT denied", f"GRANT SELECT ON users TO {RO_ROLE}", "42501", **p)

    # 2. As configured (role default is read-only): writes stop earlier, with the read-only error
    await attempt("as configured: INSERT tenders blocked (read-only)", insert, "25006")
    await attempt("as configured: CREATE TABLE blocked (read-only)", "CREATE TABLE verify_probe (id int)", "25006")

    # 3. Role attributes
    conn = await asyncpg.connect(dsn)
    try:
        elevated = await conn.fetchval(
            "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        record("role is not superuser/createrole/createdb/bypassrls", "False", str(elevated))
        for column in ("matched_services", "eligibility_gaps", "suggested_next_steps"):
            found = {r["t"] for r in await conn.fetch(
                f"SELECT DISTINCT json_typeof({column}) AS t FROM tender_analyses")}
            record(f"json shape of {column}", "{'array'}", str(found) if found else "no rows")
    finally:
        await conn.close()

    # 4. The service's own engine settings and tools against real Postgres
    engine = make_engine(url)
    try:
        async with engine.connect() as c:
            record("engine: default_transaction_read_only", "on",
                   (await c.execute(text("SHOW default_transaction_read_only"))).scalar())
            record("engine: statement_timeout", "5s", (await c.execute(text("SHOW statement_timeout"))).scalar())
        async with make_session_factory(engine)() as session:
            result = await tools.search_tenders(session, include_mock=True, include_unreviewed=True, limit=25)
            record("tools: search_tenders runs through the read-only role", "True",
                   str(bool(result.get("ok") and result.get("count", 0) > 0)))
    finally:
        await engine.dispose()

    # 5. Seatbelt probe: a role that CAN insert but has the read-only default (scratch role, dropped after)
    admin = create_async_engine(admin_url())
    probe_password = secrets.token_urlsafe(24)
    try:
        async with admin.begin() as c:
            await c.execute(text(f"DROP ROLE IF EXISTS {PROBE_ROLE}"))
            await c.execute(text(f"CREATE ROLE {PROBE_ROLE} LOGIN PASSWORD '{probe_password}'"))
            await c.execute(text(f"GRANT CONNECT ON DATABASE {DB_NAME} TO {PROBE_ROLE}"))
            await c.execute(text(f"GRANT USAGE ON SCHEMA public TO {PROBE_ROLE}"))
            await c.execute(text(f"GRANT INSERT ON tenders TO {PROBE_ROLE}"))
            await c.execute(text(f"ALTER ROLE {PROBE_ROLE} SET default_transaction_read_only = on"))
        kwargs = dict(host="127.0.0.1", port=PG_PORT, user=PROBE_ROLE, password=probe_password, database=DB_NAME)
        conn = await asyncpg.connect(**kwargs)  # fresh connection: the role setting applies at login
        try:
            try:
                await conn.execute(insert)
                got = "ok"
            except asyncpg.PostgresError as exc:
                got = exc.sqlstate
            record("seatbelt: INSERT with grant but read-only default", "25006", got)
        finally:
            await conn.close()
        conn = await asyncpg.connect(**kwargs)
        try:
            tr = conn.transaction()
            await tr.start()
            try:
                await conn.execute("SET transaction_read_only = off")
                await conn.execute(insert)
                overridable = "overridable"
            except asyncpg.PostgresError as exc:
                overridable = f"blocked ({exc.sqlstate})"
            finally:
                await tr.rollback()
            # Informational: a session can switch the seatbelt off, so grants are the real boundary.
            rows.append(("seatbelt: session can switch it off (informational)", "n/a", overridable, "INFO"))
        finally:
            await conn.close()
    finally:
        async with admin.begin() as c:
            if (await c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": PROBE_ROLE})).scalar():
                await c.execute(text(f"DROP OWNED BY {PROBE_ROLE}"))
                await c.execute(text(f"DROP ROLE {PROBE_ROLE}"))
        await admin.dispose()
    return rows


def cmd_verify(args) -> None:
    rows = asyncio.run(_verify())
    width = max(len(r[0]) for r in rows)
    for label, expected, got, verdict in rows:
        print(f"{verdict:4}  {label:<{width}}  expected={expected}  got={got}")
    failed = [r for r in rows if r[3] == "FAIL"]
    print(f"\n{sum(1 for r in rows if r[3] == 'PASS')} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


def cmd_run(args) -> None:
    values = read_env(SERVER_ENV)
    if not values:
        die(f"{SERVER_ENV} is missing; run 'up' first")
    env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    env.update(values)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    port = values.get("MCP_PORT", str(MCP_PORT))
    argv = [sys.executable, "-m", "uvicorn", "app.mcp_server.server:create_app", "--factory",
            "--host", "127.0.0.1", "--port", port, "--workers", str(args.workers),
            "--no-access-log"]  # access logs would record query strings, including rejected credentials
    print(f"serving http://127.0.0.1:{port}/mcp with {args.workers} workers (Ctrl+C to stop)")
    os.chdir(REPO_ROOT)
    os.execvpe(sys.executable, argv, env)


def cmd_check(args) -> None:
    import httpx

    values = read_env(SERVER_ENV)
    if not values:
        die(f"{SERVER_ENV} is missing; run 'up' first")
    token = values["MCP_BEARER_TOKEN"]
    base = f"http://127.0.0.1:{values.get('MCP_PORT', MCP_PORT)}/mcp"
    results: List[Tuple[str, bool, str]] = []

    def post(body, *, auth=True, headers=None, url=base, query_token=None):
        hdrs = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-11-25", "Connection": "close"}
        if auth:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        if query_token:
            url = f"{url}?access_token=abc"
        with httpx.Client(timeout=15) as client:  # a fresh connection for every request
            return client.post(url, json=body, headers=hdrs)

    def rpc(method, params=None, rid=1):
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        return body

    def check(label, ok, detail=""):
        results.append((label, bool(ok), detail))

    def payload(response):
        result = response.json()["result"]
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            text = result["content"][0]["text"]
            try:
                data = json.loads(text)
            except ValueError:
                data = {"ok": False, "error": text[:200]}
        return data["result"] if "ok" not in data and isinstance(data.get("result"), dict) else data

    try:
        r = post(rpc("tools/list"), auth=False)
        check("no token -> 401", r.status_code == 401, str(r.status_code))
        check("401 carries no WWW-Authenticate", "www-authenticate" not in {k.lower() for k in r.headers})
        r = post(rpc("tools/list"), headers={"Authorization": "Bearer wrong"})
        check("wrong token -> 401", r.status_code == 401, str(r.status_code))
        r = post(rpc("tools/list"), query_token=True)
        check("credential in query string -> 401 even with valid header", r.status_code == 401, str(r.status_code))
        r = post(rpc("tools/list"), headers={"Host": "evil.example"})
        check("unknown Host -> 421", r.status_code == 421, str(r.status_code))
        r = post(rpc("tools/list"), headers={"Origin": "https://evil.example"})
        check("unknown Origin -> 403", r.status_code == 403, str(r.status_code))
        with httpx.Client(timeout=15) as client:
            r = client.get(base, headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"})
        check("GET with valid token -> 405", r.status_code == 405, str(r.status_code))

        r = post(rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                    "clientInfo": {"name": "demo-check", "version": "1"}}))
        check("initialize -> 200, protocol 2025-11-25",
              r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-11-25", str(r.status_code))
        check("no session id issued", "mcp-session-id" not in {k.lower() for k in r.headers})
        r = post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("initialized notification -> 202", r.status_code == 202, str(r.status_code))
        r = post(rpc("tools/list"))
        names = sorted(t["name"] for t in r.json()["result"]["tools"]) if r.status_code == 200 else []
        check("exactly the three tools", names == ["explain_tender_match", "get_tender_details", "search_tenders"], str(names))

        r = post(rpc("tools/call", {"name": "search_tenders", "arguments": {"include_mock": True, "limit": 5}}, 2))
        found = payload(r) if r.status_code == 200 else {}
        check("search_tenders returns rows", found.get("ok") and found.get("count", 0) > 0, str(found.get("count")))
        first = (found.get("tenders") or [{}])[0].get("id")
        if first:
            r = post(rpc("tools/call", {"name": "get_tender_details", "arguments": {"tender_id": first}}, 3))
            check("get_tender_details works", r.status_code == 200 and payload(r).get("ok"), f"id={first}")
            r = post(rpc("tools/call", {"name": "explain_tender_match", "arguments": {"tender_id": first}}, 4))
            check("explain_tender_match works", r.status_code == 200 and payload(r).get("ok"), f"id={first}")

        ok = 0
        failures: Dict[str, int] = {}
        for i in range(args.calls):
            r = post(rpc("tools/call", {"name": "search_tenders", "arguments": {"include_mock": True, "limit": 3}}, 100 + i))
            if r.status_code == 200 and payload(r).get("ok"):
                ok += 1
            else:
                failures[str(r.status_code)] = failures.get(str(r.status_code), 0) + 1
        check(f"{args.calls} fresh-connection calls all succeed (no session errors)", ok == args.calls,
              f"ok={ok} failures={failures or 'none'}")
    except httpx.HTTPError as exc:
        check("server reachable", False, f"{type(exc).__name__}: is 'run' active in another terminal?")

    width = max(len(r[0]) for r in results)
    for label, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {label:<{width}}  {detail}")
    parents = subprocess.run(["pgrep", "-f", "uvicorn app.mcp_server.server:create_app"],
                             capture_output=True, text=True).stdout.split()
    if parents:
        kids = subprocess.run(["pgrep", "-P", parents[0]], capture_output=True, text=True).stdout.split()
        print(f"\nuvicorn parent {parents[0]} has {len(kids)} child processes (workers and helpers)")
    else:
        print("\ncould not find the uvicorn parent process (informational only)")
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


def cmd_down(args) -> None:
    result = docker("rm", "-f", CONTAINER, check=False)
    print("removed container" if result.returncode == 0 else "no container to remove")
    if args.purge:
        for path in (SERVER_ENV, ADMIN_ENV):
            if path.exists():
                path.unlink()
        print("deleted env files")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="start the throwaway Postgres and create the read-only role").set_defaults(func=cmd_up)
    seed = sub.add_parser("seed", help="load sample data or copy tenders from a SQLite file")
    seed.add_argument("--from-sqlite", metavar="PATH", help="copy tenders and tender_analyses from this SQLite database")
    seed.add_argument("--replace", action="store_true", help="wipe tenders and analyses in the demo database first")
    seed.set_defaults(func=cmd_seed)
    sub.add_parser("verify", help="prove the role boundary against real Postgres").set_defaults(func=cmd_verify)
    run = sub.add_parser("run", help="serve the MCP endpoint")
    run.add_argument("--workers", type=int, default=2)
    run.set_defaults(func=cmd_run)
    check = sub.add_parser("check", help="exercise the running endpoint")
    check.add_argument("--calls", type=int, default=40)
    check.set_defaults(func=cmd_check)
    down = sub.add_parser("down", help="remove the container")
    down.add_argument("--purge", action="store_true", help="also delete the env files")
    down.set_defaults(func=cmd_down)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    args.func(args)


if __name__ == "__main__":
    main()
