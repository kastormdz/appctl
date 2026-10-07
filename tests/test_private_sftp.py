#!/usr/bin/env python3
"""Directorio privado por proyecto y preservacion de upload: gate.

QUE VERIFICA: en los ARCHIVOS de los stacks PHP (entrypoint, nginx.conf)
que (1) el arranque JAMAS borra upload/, (2) existe /private hermano de
/upload con dueño SFTP y modo 700, (3) nginx no sirve /private ni sigue
symlinks hacia el.

QUE NO VERIFICA: el comportamiento en runtime (permisos efectivos,
403/404 reales, supervivencia al recreate). Eso se prueba en un contenedor
efimero con bind mount, como manda la regla: un gate que no ejecuta es
decorativo para lo que pasa adentro del contenedor.

Por que existe: el entrypoint tenia `rm -rf ${SFTP_CHROOT}/upload` sin
ningun `if`, y como /srv/sftp es el bind mount del disco real del host,
cada arranque (run, restart, recreate, reboot) borraba el codigo que el
cliente habia subido. Medido en bind temporal: el archivo testigo
desaparecio del host. El directorio privado se diseno despues de ese
hallazgo, con mkdir -p y prohibicion explicita de rm -rf.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHP_STACKS = ("php-postgres-sftp", "php-mysql-sftp")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS  {msg}")


def main() -> int:
    print("Directorio privado y preservacion de upload")
    entrypoints: dict[str, str] = {}
    nginxs: dict[str, str] = {}
    for stack in PHP_STACKS:
        base = ROOT / "stacks" / stack
        entrypoints[stack] = (base / "image" / "entrypoint.sh").read_text()
        nginxs[stack] = (base / "image" / "nginx.conf").read_text()

    # ---- 1. el arranque no borra nada del cliente ----
    print("\n=== el arranque preserva upload ===")
    for stack in PHP_STACKS:
        ep = entrypoints[stack]
        check(re.search(r"rm\s+-rf.*upload", ep) is None,
              f"{stack}: el entrypoint tiene un rm -rf sobre upload")
        check(re.search(r"rm\s+-rf.*SFTP_CHROOT", ep) is None,
              f"{stack}: el entrypoint tiene un rm -rf sobre el chroot")
        check('mkdir -p "${SFTP_CHROOT}/upload"' in ep,
              f"{stack}: el entrypoint crea upload con mkdir -p")

    # ---- 2. el directorio privado existe, con dueño y modo correctos ----
    print("\n=== /private: creacion y permisos ===")
    for stack in PHP_STACKS:
        ep = entrypoints[stack]
        check('PRIVATEDIR="${SFTP_CHROOT}/private"' in ep,
              f"{stack}: define /private como hermano de /upload en el chroot")
        check(re.search(r"mkdir -p \"\$\{PRIVATEDIR\}\"", ep) is not None,
              f"{stack}: crea /private con mkdir -p (nunca rm -rf)")
        check(re.search(r"chown \"\$\{SFTP_UID\}:\$\{SFTP_GID\}\" "
                        r"\"?\$\{PRIVATEDIR\}\"", ep) is not None,
              f"{stack}: /private queda del usuario SFTP (numerico, no depende "
              f"del orden de creacion)")
        check(re.search(r"chmod 700 \"\$\{PRIVATEDIR\}\"", ep) is not None,
              f"{stack}: /private queda en 700 (ni nginx ni php-fpm entran)")
        check(re.search(r"rm\s+-rf.*PRIVATEDIR", ep) is None,
              f"{stack}: ningun rm -rf toca /private")

    # ---- 3. nginx: root, deny explicito y sin symlinks ----
    print("\n=== nginx: /private no se sirve ===")
    for stack in PHP_STACKS:
        ng = nginxs[stack]
        check("root /srv/sftp/upload;" in ng,
              f"{stack}: el root sigue siendo /upload (/private queda fuera)")
        check(re.search(r"location \^~ /private/", ng) is not None,
              f"{stack}: hay location que niega /private/ aunque el root cambie")
        check("disable_symlinks on;" in ng,
              f"{stack}: no sigue symlinks (frena upload/link -> ../private)")

    # ---- 4. PHP llega por filesystem solo si los permisos dejan ----
    # Sin open_basedir, PHP PUEDE abrir /srv/sftp/private por path; lo que
    # lo frena es el 700 con dueño 1001 (php-fpm corre como app/1000).
    print("\n=== php.ini: sin jaula que oculte el problema ===")
    for stack in PHP_STACKS:
        ini = (ROOT / "stacks" / stack / "image" / "php.ini").read_text()
        activos = [l.strip() for l in ini.splitlines()
                   if l.strip() and not l.strip().startswith(";")
                   and "open_basedir" in l]
        check(not activos,
              f"{stack}: sin open_basedir activo (el aislamiento es el modo "
              f"700, auditable con stat)")

    # ---- 5. paridad entre stacks ----
    print("\n=== los dos stacks PHP, sin drift ===")
    a, b = PHP_STACKS
    check((ROOT / "stacks" / a / "image" / "nginx.conf").read_text()
          == (ROOT / "stacks" / b / "image" / "nginx.conf").read_text(),
          "nginx.conf identico en los dos stacks PHP")

    # ---- 6. documentado ----
    print("\n=== README lo documenta ===")
    for readme in ("README.md", "README.en.md"):
        t = (ROOT / readme).read_text()
        check("sftp/home/private" in t,
              f"{readme} no muestra sftp/home/private en el layout")

    print("\ntodo bien")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFALLA: {exc}")
        sys.exit(1)
