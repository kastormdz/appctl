"""Dump y restore de la base de datos de un proyecto.

Dos motores, dos herramientas, y las diferencias importan:

- Postgres: `pg_dump -Fc` (formato custom, comprime solo, se restaura con
  `pg_restore`). El cliente se conecta por el socket, no por TCP, asi que
  no necesita password. `-Fc` + `pg_restore` es el unico camino que restaura
  indices, sequences y owners bien.
- MariaDB/MySQL: `mariadb-dump --single-transaction` (consistente sin
  bloquear escritura) + `mariadb`. `--routines --triggers --events` para que
  no se pierda nada.

Por que se ejecuta DENTRO del contenedor de db y no desde el host:
el contenedor tiene las herramientas de su motor, y la db no esta
expuesta al host por diseno. Un dump desde el host necesitaria abrir el
puerto, que es justo lo que este modulo evita.

Por que el dump va a un bind mount y no a un named volume:
`destroy` borra la carpeta y el backup sale con un tar de la carpeta. Si
fuera un named volume, "backup del proyecto" seria "tar de un directorio
que no incluye el volumen", que es peor que no tener backup.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import ipaddress
import json
import shlex
import subprocess
import time
from pathlib import Path


class DbError(RuntimeError):
    """Error de base de datos, con un mensaje para el admin."""


# --- helpers ---------------------------------------------------------------

def _run(cmd: list[str], *, timeout: int = 900,
         stdin: str | None = None, text: bool = True) -> subprocess.CompletedProcess:
    """Ejecuta un comando y devuelve el resultado. No levanta excepcion.

    `text=False` para lo que devuelve BINARIO: `pg_dump -Fc` escribe un
    formato custom comprimido, y si se decodifica a str con replaces al
    re-codificar el archivo queda corrupto (pg_restore lo rechaza). El
    dump de postgres SIEMPRE va por aqui con text=False.
    """
    try:
        return subprocess.run(cmd, capture_output=True,
                              text=text, input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DbError(
            f"el comando tardo mas de {timeout}s y se corto:\n"
            f"  {shlex.join(cmd[:6])}...\n"
            f"si la base es muy grande, subilo con --timeout (en segundos)") from exc
    except FileNotFoundError as exc:
        raise DbError(f"no existe el comando: {cmd[0]}") from exc


def db_kind(stack: str) -> str:
    """'php-mysql-sftp' -> 'mysql'; el resto son postgres."""
    return "mysql" if "mysql" in stack else "postgres"


def _as_text(v) -> str:
    """stderr puede venir en bytes o str: un dump binario rompe el texto."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _stream_gzip(cmd: list[str], dest: Path, *, timeout: int) -> tuple[int, bytes, int]:
    """Ejecuta el dump y lo comprime sin cargarlo entero en RAM."""
    import selectors

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise DbError(f"no existe el comando: {cmd[0]}") from exc

    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    stderr = bytearray()
    total = 0
    started = time.monotonic()
    try:
        with gzip.open(dest, "wb", compresslevel=6) as compressed:
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    proc.kill()
                    raise DbError(
                        f"el comando tardo mas de {timeout}s y se corto; "
                        "si la base es muy grande, subilo con --timeout")
                for key, _ in selector.select(timeout=1):
                    chunk = key.fileobj.read(1 << 20)
                    if chunk:
                        if key.data == "out":
                            compressed.write(chunk)
                            total += len(chunk)
                        else:
                            stderr.extend(chunk)
                    else:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
        rc = proc.wait(timeout=5)
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return rc, bytes(stderr), total


# --- dump ------------------------------------------------------------------

def dump(project_dir: str | Path, stack: str, *, out: str | None = None,
         timeout: int = 900) -> Path:
    """Saca un dump de la base. Devuelve el path del archivo."""
    project_dir = Path(project_dir)
    kind = db_kind(stack)
    backups = project_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)

    if out:
        dest = Path(out)
        if dest.suffix != ".gz":
            dest = dest.with_name(dest.name + ".gz")
    else:
        dest = backups / f"{project_dir.name}-{_stamp()}.dump.gz"

    # El dump se escribe a STDOUT y se guarda en el host: el contenedor no
    # necesita tener acceso al disco del proyecto, y asi el archivo queda
    # con el owner de quien lo pidio, no con el del contenedor.
    if kind == "postgres":
        # -e/-u van ANTES del nombre del contenedor: olvidar ese orden
        # hace que docker ejecute "-e" como binario y el dump muere con
        # 'exec: "-e": executable file not found' (127).
        cmd = ["docker", "exec", *_pw_flag(project_dir, stack),
               f"{project_dir.name}_db",
               "pg_dump", "-U", _db_user(project_dir, stack),
               "-d", _db_name(project_dir, stack),
               "-Fc", "--no-owner", "--no-acl"]
    else:
        # El superuser de mariadb NO es root sin password: este compose crea
        # MARIADB_USER=globeteam con password. Con -u root a secas el dump
        # muere con 'Access denied for user root@localhost (using password:
        # NO)'. El usuario sale del contenedor, como en postgres.
        cmd = ["docker", "exec", "-e",
               f"MYSQL_PWD={_db_password(project_dir, stack)}",
               f"{project_dir.name}_db",
               "mariadb-dump", "-u", _db_user(project_dir, stack),
               "--single-transaction", "--routines", "--triggers",
               "--events", "--databases", _db_name(project_dir, stack)]

    # El stream va directo al gzip: una base grande no se copia entera a la
    # RAM del host antes de comprimirse.
    rc, stderr, raw_bytes = _stream_gzip(cmd, dest, timeout=timeout)
    if rc != 0:
        err = _as_text(stderr)
        raise DbError(f"el dump fallo (codigo {rc}):\n{err.strip()[:600]}")
    if raw_bytes == 0:
        raise DbError(
            f"el dump salio vacio (0 bytes). No es un backup: "
            f"la base esta vacia o el usuario no tiene permiso de lectura.")

    # Todos los dumps quedan gzip, tambien el custom de PostgreSQL (-Fc ya
    # comprime internamente, pero gzip mantiene un formato uniforme para
    # rotacion, transporte y almacenamiento).

    dest.chmod(0o600)
    _write_meta(dest, kind, project_dir.name, stack)
    return dest


def _pw_flag(project_dir: Path, stack: str) -> list[str]:
    """`docker exec -e PGPASSWORD=...` para los comandos de postgres.

    Antes no hacia falta: el pg_hba de la imagen oficial arranca con
    `local all all trust`, asi que el cliente entraba por el socket sin
    password. El init/10-roles.sh ahora reescribe el pg_hba a scram-sha-256
    (estaba con la ruta mal y nunca se aplicaba), y sin esto TODOS los
    comandos fallan con 'fe_sendauth: no password supplied'.

    Para mariadb devuelve [] porque ahi la password va con -p.
    """
    if "mysql" in stack:
        return []
    return ["-e", f"PGPASSWORD={_db_password(project_dir, stack)}"]


def _db_name(project_dir: Path, stack: str) -> str:
    """El nombre de la base.

    Se pregunta al CONTENEDOR (POSTGRES_DB / MYSQL_DATABASE) y no al .env:
    el .env es lo que appctl escribe para la app, y el superuser del
    contenedor tiene otra base por defecto. Adivinar produce
    'database "x" does not exist'.
    """
    var = "MARIADB_DATABASE" if "mysql" in stack else "POSTGRES_DB"
    v = _container_env(project_dir, var)
    if v:
        return v
    env = read_env(project_dir)
    return env.get("DB_NAME") or f"{project_dir.name}_db"


def _db_user(project_dir: Path, stack: str) -> str:
    """El usuario con el que appctl habla a la db: el SUPERUSER del motor.

    Del contenedor: POSTGRES_USER en postgres, MARIADB_USER en mariadb. Del
    .env como fallback.

    NO es DB_MIGRATION_USER: ese usuario existe para correr migraciones y
    puede no tener privilegios de lectura sobre todo. Con el aparecia
    "Access denied for user 'x_mig'@'localhost' (using password: YES)".
    """
    var = "MARIADB_USER" if "mysql" in stack else "POSTGRES_USER"
    v = _container_env(project_dir, var)
    if v:
        return v
    env = read_env(project_dir)
    return env.get("DB_USER") or "postgres"



def _admin_password(project_dir: Path, stack: str) -> str:
    """La password del usuario que devuelve `_db_admin_user`.

    NO es la misma pareja en los dos motores:
    - MariaDB: admin = usuario de MIGRACIONES -> su password es
      DB_MIGRATION_PASSWORD (el init lo crea con GRANT OPTION).
    - Postgres: admin = POSTGRES_USER (el superuser del contenedor) ->
      su password es POSTGRES_PASSWORD. Usar la de migraciones aca
      desautenticaba TODOS los `db grant` con rc=2
      ('password authentication failed for user <x>_admin'),
      porque son dos roles con dos passwords distintas.
    """
    if "mysql" not in stack:
        v = _container_env(project_dir, "POSTGRES_PASSWORD")
        if v:
            return v
        return read_env(project_dir).get("DB_ADMIN_PASSWORD",
               read_env(project_dir).get("DB_PASSWORD", ""))
    v = _container_env(project_dir, "DB_MIGRATION_PASSWORD")
    if v:
        return v
    return read_env(project_dir).get("DB_MIGRATION_PASSWORD", "")


def _container_env(project_dir: Path, var: str) -> str | None:
    """Una variable de entorno del contenedor db, leida de verdad."""
    r = _run(["docker", "exec", f"{project_dir.name}_db", "printenv", var],
             timeout=30)
    if r.returncode != 0:
        return None
    v = _as_text(r.stdout).strip()
    return v or None


def read_env(project_dir: str | Path) -> dict[str, str]:
    """Lee el .env del proyecto sin ejecutar codigo."""
    env: dict[str, str] = {}
    p = Path(project_dir) / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # las passwords generadas pueden traer '#' o '=' al final: no se
        # citan los valores ni se interpretan, se toman literales.
        env[k.strip()] = v.strip()
    return env


def _write_meta(dest: Path, kind: str, project: str, stack: str) -> None:
    """Un .json al lado del dump: sin esto, en 6 meses nadie sabe de que es."""
    meta = {
        "proyecto": project,
        "stack": stack,
        "motor": kind,
        "archivo": dest.name,
        "bytes": dest.stat().st_size,
        "sha256": _sha256(dest),
        "creado": _dt.datetime.now().isoformat(timespec="seconds"),
        "restaurar_con": (f"appctl {project} db restore {dest}"
                          if kind == "postgres"
                          else f"appctl {project} db restore {dest}"),
    }
    dest.with_suffix(dest.suffix + ".json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- restore ---------------------------------------------------------------

def _dump_bytes(src: Path) -> bytes:
    """Lee un dump viejo plano o el formato gzip actual.

    Detecta gzip por magic bytes, no por el nombre: asi se pueden restaurar
    backups anteriores (.dump/.sql) y tambien archivos gzip renombrados.
    """
    raw = src.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError as exc:
            raise DbError(f"el backup gzip esta corrupto: {src}") from exc
    return raw


def restore(project_dir: str | Path, stack: str, dump_path: str | Path,
            *, timeout: int = 900, keep_backup: bool = True,
            clean: bool = False) -> Path | None:
    """Restaura un dump. PISA la base entera.

    Por defecto saca antes un dump del estado actual: si el restore falla a
    mitad, queda el backup del estado bueno para volver. Con `keep_backup`
    en False se saltea ese seguro (mas rapido, menos seguro).
    Devuelve el backup del estado previo, o None si no se saco.

    NOTA sobre el owner: el restore entra con el superuser, asi que las
    tablas que crea quedan con owner=superuser. Para el uso de este comando
    (el admin restaura y despues el cliente se conecta con SU rol) no es
    problema, pero si alguna vez hay que traspasar las tablas al rol de la
    app hay que hacerlo explicitamente. El `clone` SI entra con el rol de
    la app, porque ahi el proyecto nuevo tiene que quedar usable por el
    cliente sin intervention.
    """
    project_dir = Path(project_dir)
    src = Path(dump_path)
    if not src.is_absolute():
        # relativo al directorio de backups del proyecto
        cand = project_dir / "backups" / dump_path
        src = cand if cand.exists() else Path(dump_path)
    if not src.exists():
        raise DbError(f"no existe el archivo: {src}")

    kind = db_kind(stack)
    safety: Path | None = None
    if keep_backup:
        try:
            safety = dump(project_dir, stack, timeout=timeout)
        except DbError as exc:
            # si no se puede respaldar, el restore igual no es seguro, pero
            # no es motivo para negarse: el dump viejo existe y el admin
            # puede decidir. Se avisa, no se bloquea.
            safety = None
            print(f"  aviso: no se pudo sacar el dump de seguridad: {exc}")

    # -Fc y las routines de mariadb necesitan su flag; --clean para que un
    # restore NO deixe tablas viejas de un dump anterior.
    if kind == "postgres":
        inner = ["pg_restore", "-U", _db_user(project_dir, stack),
                 "-d", _db_name(project_dir, stack),
                 "--no-owner", "--no-acl"]
        if clean:
            inner += ["--clean", "--if-exists"]
        # pg_restore con -Fc lee el archivo por STDIN EN BYTES: una pasada
        # por str rompe el formato custom y pg_restore lo rechaza. Y con
        # stdin=None el docker exec se queda esperando para siempre: me
        # colgo una sesion entera por dejar esa llamada de mas.
        # igual que en dump(): los flags de exec van ANTES del contenedor
        r = subprocess.run(["docker", "exec", "-i",
                            *_pw_flag(project_dir, stack),
                            f"{project_dir.name}_db", *inner],
                           input=_dump_bytes(src), capture_output=True,
                           timeout=timeout)
    else:
        db = _db_name(project_dir, stack)
        # el dump de mariadb lleva `CREATE DATABASE`/`USE`: entra por -D para
        # que no dependa del default, y las tablas se reemplazan igual.
        inner = ["mariadb", "-u", _db_user(project_dir, stack), db]
        r = subprocess.run(["docker", "exec", "-i",
                            "-e",
                            f"MYSQL_PWD={_db_password(project_dir, stack)}",
                            f"{project_dir.name}_db", *inner],
                           input=_dump_bytes(src), capture_output=True,
                           timeout=timeout)

    if r.returncode != 0:
        err = _as_text(r.stderr)
        msg = err.strip()[:600]
        extra = ""
        if safety:
            extra = (f"\n\n  El estado ANTERIOR quedo respaldado en:\n"
                     f"    {safety}\n"
                     f"  para volver atras:\n"
                     f"    appctl {project_dir.name} db restore {safety}")
        raise DbError(f"el restore fallo (codigo {r.returncode}):\n{msg}{extra}")

    return safety


# --- verificacion ----------------------------------------------------------

def tables_count(project_dir: str | Path, stack: str) -> int | None:
    """Cuantas tablas tiene la base ahora. Para comparar antes/después."""
    project_dir = Path(project_dir)
    db = _db_name(project_dir, stack)
    if db_kind(stack) == "postgres":
        cmd = ["docker", "exec",
               *_pw_flag(project_dir, stack),
               f"{project_dir.name}_db",
               "psql", "-U", _db_user(project_dir, stack), "-d", db, "-tAc",
               "select count(*) from information_schema.tables "
               "where table_schema='public'"]
    else:
        # Con credenciales: sin MYSQL_PWD MariaDB cae a root@localhost sin
        # password y devuelve Access denied; la funcion quedaba en None y
        # `db restore` imprimia 'tablas: None -> None' sin haber verificado.
        cmd = ["docker", "exec", "-e",
               f"MYSQL_PWD={_db_password(project_dir, stack)}",
               f"{project_dir.name}_db",
               "mariadb", "-u", _db_user(project_dir, stack),
               "-N", "-B", "-e",
               f"select count(*) from information_schema.tables "
               f"where table_schema='{_sql_string(db)}'"]
    r = _run(cmd, timeout=60)
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return int(out) if out.isdigit() else None


def rows_in(project_dir: str | Path, stack: str, table: str) -> int | None:
    """Filas de una tabla. Sirve para probar que el import trae datos."""
    project_dir = Path(project_dir)
    db = _db_name(project_dir, stack)
    ident = _quote_ident(table, db_kind(stack))
    if db_kind(stack) == "postgres":
        cmd = ["docker", "exec",
               *_pw_flag(project_dir, stack),
               f"{project_dir.name}_db",
               "psql", "-U", _db_user(project_dir, stack), "-d", db, "-tAc", f"select count(*) from {ident}"]
    else:
        cmd = ["docker", "exec", "-e",
               f"MYSQL_PWD={_db_password(project_dir, stack)}",
               f"{project_dir.name}_db",
               "mariadb", "-u", _db_user(project_dir, stack),
               "-N", "-B", db, "-e", f"select count(*) from {ident}"]
    r = _run(cmd, timeout=60)
    out = (r.stdout or "").strip()
    return int(out) if out.isdigit() else None


def _quote_ident(name: str, kind: str) -> str:
    """Un nombre de tabla va citado: 'usuarios' vs 'users'."""
    if kind == "postgres":
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"

# --- grants: dar acceso a un usuario externo ------------------------------
#
# Postgres y MariaDB hacen esto de forma distinta y NO se pueden unificar:
#
# - MariaDB: el origen de conexion va DENTRO del GRANT
#   (`GRANT ... TO user@'10.%'`). El usuario se define con su host.
# - Postgres: el GRANT es solo del privilegio; el ORIGEN va aparte en
#   pg_hba.conf (que HAY que editar y recargar) y el usuario se crea con
#   CREATE ROLE. Sin la linea en pg_hba.conf el GRANT no sirve de nada:
#   postgres responde 'no pg_hba.conf entry ...'.
#
# Por eso un solo comando con dos caminos, y por eso la verificacion
# importa: un GRANT que no se prueba desde el host es una promesa.


def grant(project_dir: str | Path, stack: str, *, user: str, password: str,
          cidr: str, role: str = "readonly", schema: str = "public",
          migrate: bool = False) -> str:
    """Crea un usuario y le da acceso a la base. Devuelve un resumen.

    `cidr` es el origen permitido: '10.20.0.0/16', '192.168.1.%' (mysql),
    '0.0.0.0/0' para cualquiera (con lo que eso implica).
    """
    project_dir = Path(project_dir)
    kind = db_kind(stack)
    _validate_ident(user)
    if not password or len(password) < 8:
        raise DbError(
            f"la password de {user} es muy corta (min 8).\n"
            f"  una de 4 caracteres se adivina mirando el log de la app.")
    role_norm = _norm_role(role)
    _validate_ident(schema)
    cidr_norm = _normalize_cidr(cidr, kind)

    if kind == "mysql":
        return _grant_mysql(project_dir, stack, user, password, cidr_norm, role_norm)
    return _grant_postgres(project_dir, stack, user, password, cidr_norm,
                           role_norm, schema, migrate)


def revoke(project_dir: str | Path, stack: str, *, user: str) -> str:
    """Saca el acceso de un usuario. En postgres borra la linea de pg_hba."""
    project_dir = Path(project_dir)
    _validate_ident(user)
    if db_kind(stack) == "mysql":
        host = _mysql_host_of(project_dir, user)
        sql = (f"DROP USER IF EXISTS '{_mysql_string(user)}'@"
               f"'{_mysql_string(host)}'")
        r = _sql(project_dir, stack, [sql])
        return f"mysql: {sql}" + _err(r)
    # postgres, en este orden:
    #   1. sacarlo de pg_hba.conf, o queda una linea que permite entrar a un
    #      rol que ya no existe (y el error del proximo es críptico)
    #   2. DROP OWNED BY: se lleva los objetos Y los default privileges que
    #      el grant creo. Sin esto, DROP ROLE falla con 'cannot be dropped
    #      because some objects depend on it' y el admin no sabe que hacer.
    #   3. por ultimo el rol, que ya no tiene nada colgando.
    _pg_hba_remove(project_dir, user)
    db = _db_name(project_dir, stack)
    r = _sql(project_dir, stack, [
        f'DROP OWNED BY "{user}" CASCADE',
        f'DROP ROLE IF EXISTS "{user}"',
    ], db=db, admin=True)
    return f"postgres: DROP OWNED BY + DROP ROLE {user}" + _err(r)


# --- validacion ------------------------------------------------------------

def _normalize_cidr(cidr: str, kind: str) -> str:
    """Valida el origen y lo lleva al formato del motor sin aceptar SQL."""
    value = cidr.strip()
    if not value:
        raise DbError("el origen (--cidr) no puede estar vacio")
    if kind == "postgres":
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise DbError(f"CIDR invalido: {cidr!r}") from exc
        # El usuario escribe el CIDR tal cual en pg_hba.conf, y ahi
        # PostgreSQL no normaliza: 10.0.0.5/8 (host bits en 1) se rechaza
        # en el reload con un error que no dice que el problema es el CIDR
        # del GRANT. Se exige la forma canonica de la red.
        if "/" in value and net.network_address != ipaddress.ip_address(value.split("/", 1)[0]):
            raise DbError(
                f"CIDR con host bits en 1: {cidr!r}\n"
                f"  la red es {net.network_address}/{net.prefixlen}; "
                f"escribila asi.")
        if net.prefixlen == 32:
            return str(net.network_address)
        return str(net)

    # MariaDB no entiende CIDR en la columna Host: usa '%' o comodines por
    # octeto. Convertimos solo redes IPv4 alineadas a octeto para no mentir
    # sobre el alcance permitido.
    if value in ("%", "any", "0.0.0.0/0"):
        return "%"
    if "/" not in value:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise DbError(f"origen MariaDB invalido: {cidr!r}")
    if net.version != 4 or net.prefixlen % 8:
        raise DbError("MariaDB requiere un host, comodines por octeto o una red IPv4 /8, /16 o /24")
    # Igual que en postgres: el CIDR con host bits en 1 NO se normaliza
    # silenciosamente a la red. 10.0.0.5/8 escribiria '10.%' (red /8) y el
    # usuario no seenteraria. Se exige la forma canonica.
    if "/" in value and net.network_address != ipaddress.ip_address(value.split("/", 1)[0]):
        raise DbError(
            f"CIDR con host bits en 1: {cidr!r}\n"
            f"  la red es {net.network_address}/{net.prefixlen}; escribila asi.")
    octets = str(net.network_address).split(".")[:net.prefixlen // 8]
    return ".".join(octets) + ".%"


def _sql_string(value: str) -> str:
    """Cita un literal SQL de PostgreSQL (standard_conforming_strings)."""
    return value.replace("'", "''")


def _mysql_string(value: str) -> str:
    """Cita un literal SQL de MariaDB, incluyendo backslashes."""
    return value.replace("\\", "\\\\").replace("'", "''")


def _validate_ident(name: str) -> None:
    """Un nombre de rol va a un GRANT: se cita, pero un caracter raro en el
    nombre es casi siempre un error de tipeo o un intento de inyectar."""
    if not name:
        raise DbError("el usuario esta vacio")
    # `--` abre un comentario en SQL: `a--b` citado sigue siendo un
    # identificador para postgres pero rompe la logica de cualquiera que
    # arme SQL pegando. Se rechaza entero.
    if "--" in name or "/*" in name:
        raise DbError(f"el nombre {name!r} tiene '--' o '/*': "
                      f"eso abre un comentario en SQL")
    bad = set(name) - set("abcdefghijklmnopqrstuvwxyz"
                          "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
    if bad:
        raise DbError(f"el nombre {name!r} tiene caracteres no permitidos: "
                      f"{sorted(bad)}\n"
                      f"  se permiten letras, numeros, punto, guion y guion bajo")


def _norm_role(role: str) -> str:
    r = role.lower().strip()
    # Sin alias "admin": quien pide admin espera MAS de lo que readonly/write
    # dan, y un mapping silencioso hace que un externo con admin termine
    # con menos de lo que creia. Se rechaza y se le dice que elija.
    alias = {"lectura": "readonly", "ro": "readonly", "leer": "readonly",
             "escritura": "write", "rw": "write", "app": "write",
             "migracion": "migrate", "mig": "migrate"}
    r = alias.get(r, r)
    if r not in ("readonly", "write", "migrate"):
        raise DbError(f"rol desconocido: {role}\n"
                      f"  validos: readonly (default), write, migrate")
    return r


def _err(r) -> str:
    """El mensaje de error del comando.

    mariadb ESCUPE sus errores a stdout (con el banner de version encima),
    no a stderr: por eso un GRANT fallido decía "mariadb dice: (nada)" con
    el error entero en stdout. Se lee de los dos.
    """
    if r.returncode == 0:
        return ""
    txt = (_as_text(r.stderr).strip() + "\n" + _as_text(r.stdout).strip()).strip()
    # sacar el banner de mariadb, que no es un error
    lineas = [l for l in txt.splitlines()
              if l and not l.startswith("Copyright") and "MariaDB" not in l
              and not l.startswith("mysql ") and "for " not in l]
    return f"\n  ERROR: {' | '.join(lineas)[:300] or '(sin mensaje)'}"


def _container_env_of(container: str, var: str) -> str:
    """El valor de una variable de entorno de un contenedor, leido de verdad.

    Del CONTENEDOR, no del .env: el .env es lo que appctl escribe para la
    app, y el superuser de la db tiene otros nombres (POSTGRES_USER es el
    nombre del proyecto, no 'postgres'). Adivinarlo daba
    'role X does not exist'.
    """
    if not container:
        return ""
    r = subprocess.run(["docker", "exec", container, "printenv", var],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _db_admin_user(project_dir: Path, stack: str) -> str:
    """El usuario con el que se hacen los cambios de PRIVILEGIOS.

    NO es el mismo en los dos motores:

    - MariaDB: el usuario de MIGRACIONES, que es el unico con GRANT OPTION
      (CREATE USER exige privilegios globales; ALL PRIVILEGES sobre la base
      no alcanza). Es el que el init crea con GRANT OPTION.
    - Postgres: el SUPERUSER (POSTGRES_USER). No hay equivalente a GRANT
      OPTION: cualquier rol con CREATE puede crear roles, asi que el
      admin de postgres es el dueno de la base.

    Usar el de migraciones en postgres daba 'role acme_mig does not exist':
    el init de postgres NO crea ese rol (crea app y ro), porque ahi no hace
    falta para lo que hace appctl.
    """
    if "mysql" not in stack:
        return _db_user(project_dir, stack)     # postgres: el superuser
    env = read_env(project_dir)
    return env.get("DB_MIGRATION_USER") or _db_user(project_dir, stack)


def _db_password(project_dir: Path, stack: str) -> str:
    """La password del superuser O DEL USUARIO DE MIGRACIONES.

    Son dos usuarios con dos passwords: `_db_password` devuelve la del
    usuario que devuelve `_db_user`, y si el grant usa el de migraciones
    necesita la OTRA. Por eso `_admin_password()`.
    """
    """La password del superuser de la db.

    Del CONTENEDOR (POSTGRES_PASSWORD / MARIADB_PASSWORD): es lo que el
    compose le pasa de verdad. Del .env como fallback.

    Se pasa por el entorno del `docker exec` (MYSQL_PWD / PGPASSWORD), no en
    la linea de comandos: ahi queda visible en `ps` para cualquiera del
    host, y un grant no puede dejar la password de la db en un log.
    """
    var = "MARIADB_PASSWORD" if "mysql" in stack else "POSTGRES_PASSWORD"
    v = _container_env(project_dir, var)
    if v:
        return v
    return read_env(project_dir).get("DB_PASSWORD", "")


def _sql(project_dir: Path, stack: str, sqls: list[str], *, db: str = "",
         admin: bool = False):
    """Ejecuta SQL dentro del contenedor db.

    `admin=True` usa el usuario de MIGRACIONES, que es el unico con
    GRANT OPTION (y por lo tanto el unico que puede CREATE USER). El
    usuario de la app tiene SELECT/INSERT/UPDATE/DELETE a proposito y no
    debe poder crear usuarios ni cambiar el schema.
    """
    kind = db_kind(stack)
    name = db or _db_name(project_dir, stack)
    user = _db_admin_user(project_dir, stack) if admin else _db_user(project_dir, stack)
    # La password va por el entorno del exec (MYSQL_PWD / PGPASSWORD), no en
    # la linea de comandos: en la linea queda en `ps` dentro del host y un
    # `docker exec` de otro usuario la lee. ON_ERROR_STOP para postgres:
    # sin el, psql sale con 0 aunque un CREATE haya fallado y el grant
    # "funciona" a medias.
    if kind == "mysql":
        # -e ES OBLIGATORIO: sin el, el SQL va como argumento posicional
        # despues de la base y mariadb lo toma por una opcion:
        #   Usage: mariadb [OPTIONS] [database]
        # (que es lo que pasaba: el grant fallaba con un error de uso y no
        # de sql)
        cmd = ["docker", "exec", "-e",
               f"MYSQL_PWD={(_admin_password(project_dir, stack) if admin else _db_password(project_dir, stack))}",
               f"{project_dir.name}_db",
               "mariadb", "-u", user, name, "-e"]
    else:
        cmd = ["docker", "exec", "-e",
               f"PGPASSWORD={(_admin_password(project_dir, stack) if admin else _db_password(project_dir, stack))}",
               f"{project_dir.name}_db",
               "psql", "-U", user, "-d", name, "-v", "ON_ERROR_STOP=1", "-c"]
    r = None
    for s in sqls:
        r = subprocess.run([*cmd, s], capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return r
    return r


# --- MariaDB: el origen va DENTRO del GRANT --------------------------------

def _mysql_host_of(project_dir: Path, user: str) -> str:
    """El host (% o IP) con el que se creo el usuario."""
    r = subprocess.run(
        ["docker", "exec", f"{project_dir.name}_db", "mariadb", "-N", "-B",
         "-u", _db_user(project_dir, "php-mysql-sftp"),
         "-e", f"select host from mysql.user where user='{_mysql_string(user)}' limit 1"],
        capture_output=True, text=True, timeout=60)
    return (r.stdout or "").strip() or "%"


def _grant_mysql(project_dir: Path, stack: str, user: str, pw: str,
                 cidr: str, role: str) -> str:
    host = "%" if cidr in ("%", "0.0.0.0/0", "any") else cidr
    # En MariaDB el usuario se define con user@host. Si existe con otro host,
    # se borra: tener el mismo nombre en dos hosts es una confusion que
    # nadie va a debuggear.
    db = _db_name(project_dir, stack)
    prev = _mysql_host_of(project_dir, user)
    sqls = []
    if prev != host:
        sqls.append(f"DROP USER IF EXISTS '{_mysql_string(user)}'@'{_mysql_string(prev)}'")
    sqls.append(f"CREATE USER IF NOT EXISTS '{_mysql_string(user)}'@'{_mysql_string(host)}' IDENTIFIED BY '{_mysql_string(pw)}'")
    if role == "readonly":
        sqls.append(f"GRANT SELECT ON `{db}`.* TO '{_mysql_string(user)}'@'{_mysql_string(host)}'")
    elif role == "write":
        sqls.append(f"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, "
                    f"ALTER, INDEX, REFERENCES ON `{db}`.* TO '{_mysql_string(user)}'@'{_mysql_string(host)}'")
    else:   # migrate
        sqls.append(f"GRANT ALL PRIVILEGES ON `{db}`.* TO '{_mysql_string(user)}'@'{_mysql_string(host)}'")
    sqls.append("FLUSH PRIVILEGES")
    # _sql devuelve en el primer fallo, sin decir cual fue: un error de
    # mysql sin stderr y sin el SQL es imposible de debuggear. Se ejecuta
    # aca, uno por uno, y se sabe exactamente que sentencia fallo.
    for s in sqls:
        r = _sql(project_dir, stack, [s], admin=True)
        if r.returncode != 0:
            detalle = _err(r).strip()
            hint = ""
            if "1045" in detalle or "Access denied" in detalle:
                hint = (
                    "\n  MariaDB necesita CREATE USER, que exige privilegios "
                    "GLOBALES.\n"
                    "  El usuario de migraciones los tiene si el init del "
                    "stack corRIO con GRANT OPTION.\n"
                    "  Si este proyecto es ANTERIOR a eso, no hay forma de "
                    "darlo desde appctl:\n"
                    "    el root de mariadb no es alcanzable con "
                    "DB_ROOT_PASSWORD\n"
                    "    (la db se inicializo antes de que appctl "
                    "generara esa clave).\n"
                    "  Salida: recrear el proyecto para que corra el init.\n"
                    f"    appctl {project_dir.name} destroy\n"
                    f"    appctl {project_dir.name} php mysql sftp\n"
                    "  (BORRA los datos. hacer un dump antes:\n"
                    f"    appctl {project_dir.name} db dump)")
            raise DbError(
                f"el GRANT de mysql fallo en:\n  {s[:120]}\n"
                f"  {detalle}{hint}")
    return (f"mariadb: {user}'@'{host} -> {db} ({role})\n"
            f"  conectar: mysql -h <host_srv> -u {user} -p --database={db}\n"
            f"  el origen queda en el GRANT: MySQL no tiene pg_hba.")


# --- Postgres: GRANT + pg_hba.conf -----------------------------------------

HBA_MARK = "# appctl:"


def _pg_hba_file(project_dir: Path) -> str:
    """Ruta del pg_hba.conf DENTRO del contenedor.

    Se la PREGUNTA al contenedor (PGDATA) y no se hardcodea: este compose
    monta ./db/data:/var/lib/postgresql/data y define
    PGDATA=/var/lib/postgresql/data/pgdata, asi que el archivo real esta en
    .../data/pgdata/pg_hba.conf. Con la ruta fija el grant hacia el append
    sobre un archivo inexistente y el reload no confirmaba nada.
    """
    r = _run(["docker", "exec", f"{project_dir.name}_db", "printenv", "PGDATA"],
             timeout=30)
    pgdata = _as_text(r.stdout).strip()
    return (f"{pgdata}/pg_hba.conf" if pgdata
            else "/var/lib/postgresql/data/pg_hba.conf")


def _pg_hba_read(project_dir: Path) -> str:
    r = subprocess.run(["docker", "exec", f"{project_dir.name}_db",
                        "cat", _pg_hba_file(project_dir)],
                       capture_output=True, text=True, timeout=60)
    return r.stdout or ""


def _pg_hba_remove(project_dir: Path, user: str) -> None:
    """Saca las lineas de appctl de ese usuario y recarga.

    Tolera que el archivo no exista todavia: revocar un usuario que nunca
    tuvo grant no tiene que fallar.
    """
    hba = _pg_hba_file(project_dir)
    r = subprocess.run(
        ["docker", "exec", f"{project_dir.name}_db", "sh", "-c",
         f"if [ -f {hba} ]; then "
         f"grep -v '{HBA_MARK} {user} ' {hba} > /tmp/hba.new && "
         f"cat /tmp/hba.new > {hba}; fi"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise DbError(f"no se pudo editar pg_hba.conf ({hba}):\n"
                      f"{_as_text(r.stderr).strip()[:300] or _as_text(r.stdout).strip()[:300]}")
    # -d es OBLIGATORIO: sin el, psql conecta a una base con el nombre del
    # usuario (acme) que no existe, y el reload nunca corre. El mensaje de
    # error era 'database "acme" does not exist'.
    # PGPASSWORD (via _pw_flag) tambien: con pg_hba en scram, un psql sin
    # password muere con 'fe_sendauth: no password supplied' y el revoke
    # dejaba la linea sacada PERO el reload sin aplicar hasta el proximo.
    subprocess.run(["docker", "exec",
                    *_pw_flag(project_dir, "php-postgres-sftp"),
                    f"{project_dir.name}_db", "sh", "-c",
                    "psql -U $(printenv POSTGRES_USER) "
                    "-d $(printenv POSTGRES_DB) "
                    "-tAc 'SELECT pg_reload_conf()'"],
                   capture_output=True, timeout=60)


def _grant_postgres(project_dir: Path, stack: str, user: str, pw: str,
                    cidr: str, role: str, schema: str, migrate: bool) -> str:
    db = _db_name(project_dir, stack)
    su = _db_user(project_dir, stack)
    # password en la linea de comandos: visible en `ps` dentro del contenedor
    # unos segundos. Es aceptable para un admin con sudo en el server, pero
    # se dice en el mensaje final.
    sqls = [
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
        f"WHERE rolname='{_sql_string(user)}') THEN CREATE ROLE \"{user}\" LOGIN; "
        f"END IF; END $$;",
        f"ALTER ROLE \"{user}\" WITH PASSWORD '{_sql_string(pw)}'",
        f"GRANT CONNECT ON DATABASE \"{db}\" TO \"{user}\"",
        f"GRANT USAGE ON SCHEMA \"{schema}\" TO \"{user}\"",
    ]
    if role == "readonly":
        sqls.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA \"{schema}\" "
                    f'TO "{user}"')
        sqls.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA \"{schema}\" "
                    f'GRANT SELECT ON TABLES TO "{user}"')
    elif role == "write":
        sqls.append(f"GRANT SELECT, INSERT, UPDATE, DELETE, "
                    f'USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{user}"')
        sqls.append(f"GRANT CREATE ON SCHEMA \"{schema}\" TO \"{user}\"")
        sqls.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA \"{schema}\" "
                    f'GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO "{user}"')
    else:
        sqls.append(f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA "
                    f'"{schema}" TO "{user}"')
        sqls.append(f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "
                    f'"{schema}" TO "{user}"')
        sqls.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA \"{schema}\" "
                    f'GRANT ALL PRIVILEGES ON TABLES TO "{user}"')

    r = _sql(project_dir, stack, sqls)
    if r.returncode != 0:
        raise DbError(f"el GRANT de postgres fallo:\n{_as_text(r.stderr)[:500]}")

    # --- pg_hba: SIN esta linea el GRANT no sirve ----------------------
    metodo = "scram-sha-256"
    hba = _pg_hba_file(project_dir)
    linea = f"{HBA_MARK} {user} {cidr} {metodo}"
    # Verificar que el archivo existe ANTES de escribir. Con la ruta fija
    # (…/data/pg_hba.conf) el append creaba un archivo que postgres nunca
    # leia: PGDATA es …/data/pgdata, un nivel mas adentro. El grant
    # "funcionaba" y nadie podia entrar.
    _ex = subprocess.run(
        ["docker", "exec", f"{project_dir.name}_db", "test", "-f", hba],
        capture_output=True, timeout=30)
    if _ex.returncode != 0:
        raise DbError(
            f"no existe {hba} dentro del contenedor.\\n"
            f"  PGDATA mal montado, o el contenedor no inicializo la db.\\n"
            f"  appctl {project_dir.name} logs --service db")
    w = subprocess.run(
        ["docker", "exec", f"{project_dir.name}_db", "sh", "-c",
         f"grep -q '^{HBA_MARK} {user} ' {hba} && "
         f"sed -i 's|^{HBA_MARK} {user} .*|{linea}|' {hba} || "
         f"echo '{linea}' >> {hba}"],
        capture_output=True, text=True, timeout=60)
    if w.returncode != 0:
        raise DbError(f"no se pudo escribir {hba}:\\n{_as_text(w.stderr)[:300]}")
    v = subprocess.run(
        ["docker", "exec", f"{project_dir.name}_db", "grep", "-c",
         f"{HBA_MARK} {user} ", hba],
        capture_output=True, text=True, timeout=30)
    if (v.stdout or "0").strip() in ("0", ""):
        raise DbError(
            f"la linea de pg_hba no quedo escrita ({hba}).\\n"
            f"  el GRANT esta hecho pero nadie podria entrar.")
    rr = subprocess.run(
        ["docker", "exec",
         *_pw_flag(project_dir, stack),
         f"{project_dir.name}_db", "sh", "-c",
         "psql -U $(printenv POSTGRES_USER) -d $(printenv POSTGRES_DB) "
         "-tAc 'SELECT pg_reload_conf()'"],
        capture_output=True, text=True, timeout=60)
    if "t" not in (rr.stdout or ""):
        raise DbError(
            "pg_reload_conf() no confirmo:\n" + _as_text(rr.stderr)[:300] +
            "\n  el GRANT esta hecho pero postgres no lo tiene en cuenta.\n"
            "  revisar: docker logs <proyecto>__db | grep -i hba")

    return (f"postgres: rol {user} -> {db} ({role})\n"
            f"  pg_hba.conf: {cidr} scram-sha-256\n"
            f"  conectar: psql -h <host_srv> -U {user} -d {db}\n"
            f"  (sin la linea de pg_hba el GRANT no alcanza: es lo que decide "
            f"de DONDE se puede entrar)")
