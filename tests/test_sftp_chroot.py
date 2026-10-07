#!/usr/bin/env python3
"""Chroot minimalista del SFTP: gate.

QUE VERIFICA: en los ARCHIVOS de los stacks PHP (entrypoint, sshd_config)
que el chroot que ve el cliente trae solo /upload y /private (+ /dev,
tecnico) y ningun andamiaje de binarios. /tmp no se crea mas: era
pasajero; en proyectos viejos se quita solo si esta vacio (rmdir).

QUE NO VERIFICA: el listado real por SFTP ni que el login siga andando
despues del cambio. Eso se prueba con login SFTP contra un contenedor
efimero, como manda la regla: lo que pasa adentro del contenedor no lo
prueba un grep.

Por que existe: el SFTP corre con ForceCommand internal-sftp, EN el
proceso de sshd, asi que jamas necesito binarios en el chroot. Pero el
entrypoint copiaba sftp-server + loader + .so + shells + /etc/passwd de la
epoca del subsystem externo, y el cliente veia bin/, dev/, etc/, lib/,
proc/ y usr/ al entrar. Se saco el andamiaje y el entrypoint limpia esos
directorios en proyectos viejos (lista explicita, nunca comodines).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHP_STACKS = ("php-postgres-sftp", "php-mysql-sftp")
# Andamiaje que no puede existir mas en el chroot: ni creado ni limpiado
# con comodines. upload/private/tmp/dev son del cliente o tecnicos.
ANDAMIAJE = ("bin", "usr", "lib", "etc", "proc")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS  {msg}")


def main() -> int:
    print("Chroot minimalista del SFTP")
    entrypoints: dict[str, str] = {}
    sshd: dict[str, str] = {}
    for stack in PHP_STACKS:
        base = ROOT / "stacks" / stack
        entrypoints[stack] = (base / "image" / "entrypoint.sh").read_text()
        sshd[stack] = (base / "image" / "sshd_config").read_text()

    # ---- 1. subsystem en proceso, nada externo ----
    print("\n=== sshd_config: SFTP en proceso ===")
    for stack in PHP_STACKS:
        conf = sshd[stack]
        check("Subsystem sftp internal-sftp" in conf,
              f"{stack}: el subsystem no es internal-sftp")
        check("sftp-server" not in conf.replace("internal-sftp", ""),
              f"{stack}: sshd_config todavia nombra el binario externo")

    # ---- 2. el entrypoint no mete binarios al chroot ----
    print("\n=== entrypoint: sin andamiaje ===")
    for stack in PHP_STACKS:
        ep = entrypoints[stack]
        check("SFTP_BIN" not in ep,
              f"{stack}: todavia resuelve el binario sftp-server")
        for d in ("usr", "lib", "bin"):
            check(f"SFTP_CHROOT}}/{d}\"" not in ep,
                  f"{stack}: todavia crea {d}/ en el chroot")
        check('SFTP_CHROOT}/etc"' not in ep,
              f"{stack}: todavia crea etc/ en el chroot")
        check("ldd " not in ep,
              f"{stack}: todavia caza .so para el chroot")
        check("/usr/lib/ssh/sftp-server" not in ep,
              f"{stack}: todavia usa el binario externo en el chroot")
        check('chroot "${SFTP_CHROOT}"' not in ep,
              f"{stack}: todavia prueba binarios con chroot")

    # ---- 3. limpieza de proyectos viejos, explicita y acotada ----
    print("\n=== limpieza: lista cerrada, sin comodines ===")
    for stack in PHP_STACKS:
        ep = entrypoints[stack]
        check("for _rm in bin usr lib etc proc" in ep,
              f"{stack}: la limpieza no lista el andamiaje explicito")
        check("${SFTP_CHROOT:?}" in ep,
              f"{stack}: el rm de limpieza va sin guardia :?")
        for cliente in ("upload", "private", "tmp", "dev"):
            check(f"_rm in" not in ep or cliente not in
                  ep.split("for _rm in")[1].split("\n")[0],
                  f"{stack}: {cliente}/ cayo en la lista de borrado")
        check('mkdir -p "${SFTP_CHROOT}/dev"' in ep,
              f"{stack}: no crea /dev del chroot")
        check('SFTP_CHROOT}/tmp"' not in ep.replace('rmdir "${SFTP_CHROOT}/tmp"', ""),
              f"{stack}: todavia crea /tmp en el chroot (es pasajero, fuera)")
        check('rmdir "${SFTP_CHROOT}/tmp"' in ep,
              f"{stack}: no quita el /tmp viejo vacio (solo rmdir, nunca rm -rf)")
        check('SFTP_CHROOT}/proc' not in ep,
              f"{stack}: todavia crea proc/ en el chroot")

    # ---- 4. lo que sostiene el login sigue intacto ----
    print("\n=== el login no se rompe ===")
    for stack in PHP_STACKS:
        ep = entrypoints[stack]
        check("ForceCommand internal-sftp -d /upload" in ep,
              f"{stack}: falta el ForceCommand que para al cliente en /upload")
        check("Subsystem sftp internal-sftp" in ep,
              f"{stack}: no verifica el subsystem en proceso")
        check("mknod" in ep,
              f"{stack}: no crea los device nodes (/dev/null)")
        check('rm -rf "${SFTP_CHROOT}/upload"' not in ep,
              f"{stack}: volvio el rm -rf sobre upload")

    # ---- 5. paridad entre stacks ----
    print("\n=== los dos stacks PHP, sin drift ===")
    a, b = PHP_STACKS
    check(sshd[a] == sshd[b],
          "sshd_config difiere entre los dos stacks PHP")

    print("\ntodo bien")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFALLA: {exc}")
        sys.exit(1)
