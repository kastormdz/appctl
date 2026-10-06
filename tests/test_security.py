#!/usr/bin/env python3
"""Regresiones para las fronteras de entrada de appctl."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
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

    # Los dumps nuevos son gzip; restore sigue aceptando los dumps planos
    # anteriores para no dejar backups existentes inutilizables.
    with tempfile.TemporaryDirectory(prefix="appctl-gzip-") as td:
        gz = Path(td) / "backup.dump.gz"
        gz.write_bytes(__import__("gzip").compress(b"backup-v1\n"))
        check(dbtool._dump_bytes(gz) == b"backup-v1\n",
              "restore descomprime backups gzip")
        plain = Path(td) / "backup.sql"
        plain.write_bytes(b"legacy\n")
        check(dbtool._dump_bytes(plain) == b"legacy\n",
              "restore conserva compatibilidad con backups planos")
        streamed = Path(td) / "stream.dump.gz"
        rc, stderr, raw_bytes = dbtool._stream_gzip(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 1000000)"],
            streamed, timeout=30)
        check(rc == 0 and not stderr and raw_bytes == 1000000,
              "dump comprime sin cargar todo el stream en memoria")
        check(dbtool._dump_bytes(streamed) == b"x" * 1000000,
              "el gzip del dump se puede restaurar")

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

    # --- un proyecto que este usuario no puede leer ---
    # Bug real: 'sudo appctl create' deja el directorio 0700 root:root. El
    # listado decia 'no hay proyectos' con dos puertos registrados y un stack
    # andando, porque exists() sobre un directorio ajeno devuelve False sin
    # fallar. Decir que no hay es peor que no listar: hay que decir que no se
    # puede leer, y como se arregla.
    print("=== proyectos que no puedo leer ===")
    if os.geteuid() != 0:
        site = Path(tempfile.mkdtemp())
        raiz = site / "proyectos"
        ajeno = raiz / "ajeno"
        ajeno.mkdir(parents=True)
        (ajeno / "state.json").write_text(
            '{"project": "ajeno", "stack": "php-postgres-sftp"}\n')
        os.chmod(ajeno, 0o000)
        try:
            r = subprocess.run(
                [sys.executable, str(ROOT / "bin/appctl"), "list"],
                env=dict(os.environ, APPCTL_PROJECTS=str(raiz),
                         APPCTL_HOME=str(site / "home")),
                capture_output=True, text=True)
            out = r.stdout + r.stderr
            check("no dice 'no hay proyectos' cuando hay uno ilegible"
                  not in out,
                  "el listado reporta ausencia donde hay un proyecto que no "
                  "puede leer: " + out[:200])
            check("ajeno" in out and "leer" in out,
                  "no nombra el proyecto ilegible ni dice que no puede "
                  "leerlo: " + out[:300])
            check("chown" in out,
                  "no da el comando para arreglarlo: " + out[:300])
        finally:
            os.chmod(ajeno, 0o700)
            shutil.rmtree(site, ignore_errors=True)
    else:
        print("  (omitido: corre como root y root lee todo)")

    # --- create con sudo se niega antes de crear nada ---
    print("=== create con sudo ===")
    if os.geteuid() == 0:
        site = Path(tempfile.mkdtemp())
        raiz = site / "proyectos"
        r = subprocess.run(
            [sys.executable, str(ROOT / "bin/appctl"), "nuevo", "php", "psql",
             "sftp"],
            env=dict(os.environ, APPCTL_PROJECTS=str(raiz),
                     APPCTL_HOME=str(site / "home")),
            capture_output=True, text=True)
        out = r.stdout + r.stderr
        check(r.returncode != 0 and "sudo" in out.lower(),
              "create con root no se niega: " + out[:250])
        check("root" in out.lower(),
              "el aviso no dice por que queda como root: " + out[:250])
        check(not (raiz / "nuevo").exists(),
              "creo el directorio del proyecto pese a avisar")
        r2 = subprocess.run(
            [sys.executable, str(ROOT / "bin/appctl"), "nuevo", "php", "psql",
             "sftp", "--dry-run"],
            env=dict(os.environ, APPCTL_PROJECTS=str(raiz),
                     APPCTL_HOME=str(site / "home"), APPCTL_ALLOW_ROOT="1"),
            capture_output=True, text=True)
        check("no corras create con sudo" not in (r2.stdout + r2.stderr),
              "APPCTL_ALLOW_ROOT=1 no deja pasar")
        shutil.rmtree(site, ignore_errors=True)
    else:
        print("  (omitido: no corre como root)")

    # --- comandos globales usados con un proyecto ---
    # Bug real: `appctl acme doctor` caia al default (create), tomaba "doctor"
    # por un componente y respondia "componentes desconocidos: doctor". El
    # comando existe: lo que sobra es el nombre del proyecto.
    print("=== comandos globales con un proyecto ===")
    site = Path(tempfile.mkdtemp())
    raiz = site / "proyectos"
    for verbo in ("doctor", "build", "list", "ps", "create"):
        r = subprocess.run(
            [sys.executable, str(ROOT / "bin/appctl"), "acme", verbo],
            env=dict(os.environ, APPCTL_PROJECTS=str(raiz),
                     APPCTL_HOME=str(site / "home")),
            capture_output=True, text=True)
        out = r.stdout + r.stderr
        check(r.returncode != 0,
              f"`appctl acme {verbo}` no falla: " + out[:200])
        check("componentes desconocidos" not in out,
              f"`appctl acme {verbo}` sigue diciendo 'componentes "
              f"desconocidos': manda a buscar el problema donde no esta")
        check("global" in out and verbo in out,
              f"`appctl acme {verbo}` no explica que el comando es global: "
              + out[:200])
    check(not (raiz / "acme").exists(),
          "un comando mal usado creo el proyecto igual")
    shutil.rmtree(site, ignore_errors=True)

    # --- dry-run no deja residuo ---
    # Bug real: el render y la reserva de puertos corrian ANTES del `if
    # dry_run`, asi que un --dry-run dejaba el proyecto en disco CON el .env
    # y las credenciales, el puerto reservado, y el create de verdad despues
    # fallaba con "ya existe el proyecto". El README decia "sin tocar nada".
    print("=== dry-run sin residuo ===")
    site = Path(tempfile.mkdtemp())
    raiz = site / "proyectos"
    raiz.mkdir()
    r = subprocess.run(
        [sys.executable, str(ROOT / "bin/appctl"), "acme", "php", "psql",
         "sftp", "--dry-run"],
        env=dict(os.environ, APPCTL_PROJECTS=str(raiz),
                 APPCTL_HOME=str(site / "home")),
        capture_output=True, text=True)
    check(r.returncode == 0, "el dry-run falla: " + (r.stdout + r.stderr)[:200])
    pdir = raiz / "acme"
    check(not pdir.exists(),
          "el dry-run creo el directorio del proyecto en disco")
    check(not (pdir / ".env").exists(),
          "el dry-run escribio el .env con las credenciales")
    reg = site / "home" / "registry.json"
    if reg.exists():
        reservados = json.loads(reg.read_text()).get("ports") or {}
        check(not reservados,
              f"el dry-run reservo puertos en el registry: {reservados}")
    else:
        check(True, "el dry-run no creo el registry")
    # y la prueba de que el dano era real: el dry-run siguiente tiene que
    # poder correr igual (antes decia "ya existe el proyecto")
    r2 = subprocess.run(
        [sys.executable, str(ROOT / "bin/appctl"), "acme", "php", "psql",
         "sftp", "--dry-run"],
        env=dict(os.environ, APPCTL_PROJECTS=str(raiz),
                 APPCTL_HOME=str(site / "home")),
        capture_output=True, text=True)
    check("ya existe" not in (r2.stdout + r2.stderr),
          "un segundo dry-run dice 'ya existe el proyecto': el primero dejo "
          "residuo")
    shutil.rmtree(site, ignore_errors=True)

    print("todo bien")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
