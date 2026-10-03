#!/usr/bin/env python3
"""Regresiones para las fronteras de entrada de appctl."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import dbtool  # noqa: E402

_loader = SourceFileLoader("appctl_cli", str(ROOT / "bin" / "appctl"))
_spec = spec_from_loader("appctl_cli", _loader)
assert _spec and _spec.loader
appctl = module_from_spec(_spec)
_spec.loader.exec_module(appctl)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  PASS  {message}")


def main() -> int:
    print("Regresiones de seguridad de appctl")

    check(dbtool._normalize_cidr("10.0.0.0/8", "postgres") == "10.0.0.0/8",
          "PostgreSQL conserva una red CIDR")
    check(dbtool._normalize_cidr("10.0.0.0/8", "mysql") == "10.%",
          "MariaDB convierte CIDR alineado a comodín")
    check(dbtool._sql_string("a'b") == "a''b",
          "PostgreSQL escapa comillas")
    check(dbtool._mysql_string("a'b\\c") == "a''b\\\\c",
          "MariaDB escapa comillas y backslash")
    check("ON_ERROR_STOP=1" in appctl._restore_cmd("postgres"),
          "clone detiene el restore PostgreSQL ante errores")

    with tempfile.TemporaryDirectory(prefix="appctl-render-") as td:
        base = Path(td)
        projects = base / "projects"
        home = base / "state"
        project = projects / "acme"
        backups = project / "backups"
        backups.mkdir(parents=True)
        home.mkdir()
        rendered = base / "rendered"
        appctl.render_project(
            rendered, ROOT / "stacks" / "php-postgres-sftp",
            "php-postgres-sftp", "acme", {"php", "psql", "sftp"},
            {"php": "8.5-fpm-alpine", "psql": "18-alpine"},
            {"DB_NAME": "acme_db", "DB_USER": "acme",
             "DB_MIGRATION_USER": "acme_mig",
             "DB_READONLY_USER": "acme_ro", "SFTP_USER": "acme"},
            {"DB_PASSWORD": "app-pass", "DB_MIGRATION_PASSWORD": "mig-pass",
             "DB_READONLY_PASSWORD": "ro-pass", "SFTP_PASSWORD": "sftp-pass",
             "DB_ADMIN_PASSWORD": "admin-pass", "APP_KEY": "base64:key"},
            8001, 2221, host="example.invalid", pg_major="18",
            api_key="api-token")
        rendered_env = (rendered / ".env").read_text()
        for key in ("DB_ADMIN_PASSWORD", "APP_KEY", "API_TOKEN"):
            check(f"{key}=" in rendered_env,
                  f"render conserva {key} en .env")
        project = projects / "acme"
        (project / "state.json").write_text(json.dumps({
            "project": "acme", "stack": "php-postgres-sftp"
        }) + "\n")
        safe = backups / "safe.sql"
        safe.write_text("safe\n")

        env = os.environ.copy()
        env.update(APPCTL_PROJECTS=str(projects), APPCTL_HOME=str(home))
        rejected = subprocess.run(
            [sys.executable, str(ROOT / "bin/appctl"), "db", "acme", "rm",
             "/etc/passwd", "--yes"], env=env, capture_output=True, text=True)
        check(rejected.returncode != 0 and Path("/etc/passwd").exists(),
              "db rm rechaza una ruta absoluta")
        subprocess.run(
            [sys.executable, str(ROOT / "bin/appctl"), "db", "acme", "rm",
             "safe.sql", "--yes"], env=env, check=True,
            capture_output=True, text=True)
        check(not safe.exists(), "db rm borra un backup válido")

    print("todo bien")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
