"""Guards for the deployment files: the Docker, compose and nginx setup must stay
consistent with the app (one Python version, unprivileged container, migrations
in exactly one place, secrets kept out of the image, every setting documented)."""

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_uses_one_python_version_and_runs_unprivileged():
    dockerfile = _read("Dockerfile")
    versions = set(re.findall(r"^FROM (python:\S+)", dockerfile, re.M))
    assert versions == {"python:3.14-slim"}
    assert re.search(r"^USER app$", dockerfile, re.M)
    assert "HEALTHCHECK" in dockerfile and "/api/health" in dockerfile


def test_compose_prod_structure():
    services = yaml.safe_load(_read("docker-compose.prod.yml"))["services"]
    assert set(services) == {"db", "redis", "api", "worker", "nginx"}
    # migrations run in exactly one place, and the worker waits for the api
    assert "alembic upgrade head" in services["api"]["command"]
    assert "alembic" not in services["worker"]["command"]
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    # only nginx is reachable from outside
    for name in ("db", "redis", "api", "worker"):
        assert "ports" not in services[name], name
    assert "443:443" in services["nginx"]["ports"]
    # login rate limits need the real client IP, so uvicorn must trust the proxy
    assert "--forwarded-allow-ips" in services["api"]["command"]
    assert "DOMAIN" in services["nginx"]["environment"]


def test_nginx_template_only_needs_the_domain_variable():
    template = _read("nginx.conf.template")
    assert set(re.findall(r"\$\{(\w+)\}", template)) == {"DOMAIN"}
    assert "proxy_pass http://api:8000;" in template
    assert not (ROOT / "nginx.conf").exists()


def test_dockerignore_keeps_secrets_and_junk_out_of_the_image():
    entries = {line.strip() for line in _read(".dockerignore").splitlines()}
    assert {".env", ".git", ".venv", "frontend", "*.db"} <= entries


def test_env_example_documents_every_setting():
    from app.config import Settings

    text = _read(".env.example")
    missing = [name for name in Settings.model_fields if name not in text]
    assert not missing, f"add these to .env.example: {missing}"


def test_health_endpoint_needs_no_login(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
