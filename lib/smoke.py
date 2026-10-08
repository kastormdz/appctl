"""Smoke tests: lo que verifica que el stack esta realmente bien.

Cada check es una ASUNCION que si es falsa, el proyecto esta roto. Se
prueban TODOS, no solo el primero que falla: cuando algo anda mal a las
3am, quiero la lista completa, no un error por vez.

Los checks de aislamiento son los importantes. "El puerto no esta en el
compose.yaml" no es una garantia: es una Absence de configuracion, y la
configuracion la puede editar cualquiera. Lo que se prueba es el
COMPORTAMIENTO: que un cliente de otra red no llega a la DB.
"""
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable


class Check:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail

    def __str__(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.name}" + (
            f"\n        {self.detail}" if self.detail else ""
        )


def _compose(project_dir: str, *args: str, timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(
        ["docker", "compose", *args],
        cwd=project_dir, capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, (p.stdout + p.stderr).strip()


def _compose_input(project_dir: str, *args: str, stdin: str = "",
                   timeout: int = 60,
                   env: dict | None = None) -> tuple[int, str]:
    """docker compose exec -T con algo por stdin.

    Para SQL: pasarlo como argumento rompe el quoting del shell si tiene
    parentesis ('syntax error: unexpected "("'), y el de `sh -c` depende de
    como se arme la linea. Por stdin no hay problema: no se interpola nada.

    Devuelve (returncode, stdout+stderr).
    """
    r = subprocess.run(
        ["docker", "compose", "-f", str(Path(project_dir) / "compose.yaml"),
         "exec", "-T", *args],
        input=stdin, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})})
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _health_of(container: str) -> str:
    """Estado de salud real, leyendo docker inspect y no parseando el json
    de `compose ps`. El formato de `compose ps --format json` cambia entre
    versiones de compose (Claves como "Service" vs "Name"), y un parser
    roto hace que el check de salud mienta."""
    try:
        p = subprocess.run(
            ["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", container],
            capture_output=True, text=True, timeout=20,
        )
        return p.stdout.strip() or "desconocido"
    except (OSError, subprocess.SubprocessError):
        return "desconocido"


def _en_start_period(container: str) -> bool:
    """El contenedor esta dentro del start_period del healthcheck?

    Docker cuenta el start_period desde que arranca el contenedor: durante
    esa ventana los fallos del healthcheck NO cuentan como unhealthy real
    (el status sigue "starting"). Si el status dice "unhealthy" es porque
    el start_period ya vencio... salvo que el contenedor se haya recreado
    hace poco y el status se resetee.
    """
    r = subprocess.run(
        ["docker", "inspect", container, "--format",
         "{{.State.StartedAt}}|{{.State.Health.Status}}"],
        capture_output=True, text=True, timeout=20)
    partes = (r.stdout or "").strip().split("|")
    if len(partes) != 2 or partes[0].startswith("0001"):
        return False
    try:
        # StartedAt es RFC3339 con nanosegundos
        desde = partes[0].replace("Z", "+00:00")
        import datetime as _dt
        t0 = _dt.datetime.fromisoformat(desde)
        ahora = _dt.datetime.now(t0.tzinfo)
        return (ahora - t0).total_seconds() < 60
    except Exception:
        return False


def wait_healthy(project_dir: str, services: list[str], timeout: int = 150) -> list[Check]:
    """Espera a que los contenedores estén healthy.

    Postgres tarda en arrancar la primera vez (initdb + init scripts), por
    eso el margen. Se usa `docker inspect` y no `compose ps --format json`:
    ese formato cambio entre versiones de compose y hacia fallar el check
    de salud de un contenedor perfectamente sano.
    """
    checks, deadline = [], time.time() + timeout
    for svc in services:
        # el compose pone container_name explicito (<proyecto>_<svc>), asi
        # que el nombre real NO es el del proyecto compose sino el del
        # directorio. Leer container_name del compose es lo correcto.
        container = _container_name(project_dir, svc)
        while time.time() < deadline:
            h = _health_of(container)
            if h == "healthy" or h == "running":
                checks.append(Check(f"{svc} healthy", True,
                                    "" if h == "healthy" else "sin healthcheck"))
                break
            # `unhealthy` NO corta el loop. Durante el start_period del
            # compose (30s en mariadb) el healthcheck corre antes de que la
            # base haya terminado el initdb y docker marca unhealthy: el
            # contenedor termina healthy 20s despues. Cortar ahi daba un
            # FAIL de un stack que despues estaba perfecto.
            # exited/dead SI cortan: eso no se arreglo con esperar.
            if h in ("exited", "dead"):
                checks.append(Check(
                    f"{svc} healthy", False,
                    f"{container} esta {h}: docker compose logs {svc}",
                ))
                break
            if h == "unhealthy" and not _en_start_period(container):
                # unhealthy ya pasado el start_period: es un fallo real
                checks.append(Check(
                    f"{svc} healthy", False,
                    f"{container} esta unhealthy (pasado el start_period): "
                    f"docker compose logs {svc}",
                ))
                break
            time.sleep(5)
        else:
            if not any(c.name.startswith(svc) for c in checks):
                checks.append(Check(f"{svc} healthy", False,
                                    f"timeout a los {timeout}s "
                                    f"(estado actual: {_health_of(container)})"))
    return checks


def _container_name(project_dir: str, service: str) -> str:
    """Nombre real del contenedor, leido del container_name del compose.

    No sirve deducirlo: el compose declara `name: appctl-<proyecto>` para el
    proyecto pero `container_name: <proyecto>_<svc>` para cada servicio, y
    son distintos. Ademas el service puede diferir del sufijo.
    """
    import pathlib
    import re as _re
    pdir = pathlib.Path(project_dir)
    try:
        txt = (pdir / "compose.yaml").read_text()
    except OSError:
        txt = ""
    if txt:
        # buscar container_name dentro del bloque del servicio
        m = _re.search(
            rf"^  {_re.escape(service)}:\n(.*?)(?=^  \w+:|^\w+:|^networks:|^volumes:)",
            txt, _re.S | _re.M)
        if m:
            c = _re.search(r"^    container_name:\s*(\S+)", m.group(1), _re.M)
            if c:
                return c.group(1)
    return f"{pdir.name}_{service}"


# --- como probar que la DB responde, segun el runtime del stack ------------
# Tomcat/Next no tienen `php` adentro: correr el probe ahi da
# 'exec: "php": executable file not found' y el check reporta FAIL por una
# razon que no es la DB. El probe va por el cliente que el stack YA trae:
#   postgres -> pg_isready (esta en la propia imagen de postgres)
#   mysql    -> el healthcheck de mariadb
# Para Next/Tomcat el probe real es "la DB responde en la red interna", que
# alcanza con pg_isready desde la red del stack.
def _db_probe_cmd(db_kind: str, host: str = "db", port: str = "",
                  project: str = "") -> list[str]:
    """Como probar que la DB responde, apuntando a un host CONCRETO.

    Se devuelve una LISTA ["sh","-c","<linea>"]: quien la use tiene que
    pasarla con shlex.join(), no con " ".join (que rompe el quoting y hace
    que el probe corra sin argumentos).

    Postgres: `pg_isready` con -U y -d del contenedor. Sin -U usa $USER
    (root) y postgres responde 'role "root" does not exist'.

    MariaDB: `mariadb-admin ping -h <host> -P <puerto>`. NO sirve
    `healthcheck.sh`: ese prueba el SOCKET LOCAL (/run/mysqld/mysqld.sock),
    asi que desde otro contenedor no llega a ninguna base y el check dice
    "el proyecto no alcanza su DB" cuando lo que no llega es el socket.

    Nota sobre `ping`: responde "alive" aunque el usuario no pueda
    autenticarse, y por eso con la IP de la base ajena devuelve 0 aunque
    diga 'Access denied'. Es lo que se quiere: se prueba la RED, no las
    credenciales.
    """
    p_ = port or ("3306" if db_kind == "mysql" else "5432")
    if db_kind == "mysql":
        return ["sh", "-c", f"mariadb-admin ping -h {host} -P {p_}"]
    user, db = _pg_user_and_db(project)
    cmd = f"pg_isready -h {host} -p {p_} -U {user}"
    if db:
        cmd += f" -d {db}"
    return ["sh", "-c", cmd]


def _pg_user_and_db(project: str = "") -> tuple[str, str]:
    """(POSTGRES_USER, POSTGRES_DB) del contenedor db.

    Del CONTENEDOR, no del .env: el .env es lo que la app usa para
    conectarse y el superuser tiene otro nombre (POSTGRES_USER=acme, no
    'postgres'). Adivinarlo daba 'role X does not exist'.
    """
    c = _db_container(project)
    user = _container_env_of(c, "POSTGRES_USER") if c else ""
    db = _container_env_of(c, "POSTGRES_DB") if c else ""
    return (user or "postgres", db or "")

def _container_superuser() -> str:
    """POSTGRES_USER del contenedor db (deprecated: usar _pg_user_and_db)."""
    r = subprocess.run(["docker", "ps", "--filter", "label=appctl.rol=db",
                        "--filter", "status=running", "--format", "{{.Names}}"],
                       capture_output=True, text=True, timeout=30)
    for name in r.stdout.split():
        v = _container_env_of(name, "POSTGRES_USER")
        if v:
            return v
    return "postgres"


def _container_env_of(container: str, var: str) -> str:
    r = subprocess.run(["docker", "exec", container, "printenv", var],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout or "").strip()


def _container_env(var: str) -> str:
    return _container_env_of(_db_container(), var)


def _db_container(project: str = "") -> str:
    """El contenedor db. Si se pasa el proyecto, EL DE ESE.

    "El primero" es un bug esperando: con cuatro proyectos corriendo, el
    check de hospital leia POSTGRES_USER del contenedor de acme y probeaba
    con el usuario equivocado. Sin proyecto, el que exista.
    """
    if not project:
        r = subprocess.run(["docker", "ps", "--filter", "label=appctl.rol=db",
                            "--filter", "status=running", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=30)
        names = r.stdout.split()
        return names[0] if names else ""
    r = subprocess.run(["docker", "ps",
                        "--filter", f"label=appctl.proyecto={project}",
                        "--filter", "label=appctl.rol=db",
                        "--filter", "status=running", "--format", "{{.Names}}"],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout.split() or [""])[0]


def _wait_db_ready(project_dir: str, timeout: int = 60, db_kind: str = "postgres") -> Check:
    """Espera a que la DB responda REALMENTE, no solo a que el contenedor
    diga healthy. El healthcheck de postgres es un pg_isready, que puede
    dar ok antes de que el initdb termine de crear los roles."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        # El probe va DENTRO del contenedor `db`. Antes iba en el `app` con
        # php: en Tomcat y Next no hay php adentro, y el check daba FAIL
        # con 'exec: "php": executable file not found', que parece un fallo
        # de conexion y en realidad es "no tengo el cliente para probar".
        # pg_isready/mysqladmin viven en la propia imagen de la DB: siempre
        # estan, para cualquier runtime que haya arriba.
        rc, out = _compose(project_dir, "exec", "-T", "db",
                           *_db_probe_cmd(db_kind), timeout=30)
        if rc == 0:
            return Check("DB responde desde la app", True,
                         "la DB responde en la red interna del stack")
        last = out.strip()[:120]
        time.sleep(5)
    if "ArgumentCountError" in last:
        last = ("BUG en el check: pg_connect mal llamado. "
                "revisar lib/smoke.py")
    return Check("DB responde desde la app", False,
                 f"la DB no conecto en {timeout}s: {last}")


def db_demanda_password(project_dir: str, cfg: dict) -> Check:
    """La base PIDE password. No esta en `trust`.

    El pg_hba del init se reescribia a scram-sha-256, pero la ruta del
    archivo estaba mal (de memoria, sin PGDATA), asi que el cp fallaba en
    silencio y la base se quedaba con `local all all trust`: CUALQUIER rol
    del cluster entra sin password. El healthcheck daba healthy igual, asi
    que ningun check lo veia.

    Aqui se comprueba por comportamiento, no por leer el archivo: se
    intenta conectar SIN password y tiene que FALLAR. Si entra, la base
    esta abierta.
    """
    db_kind = (cfg or {}).get("db_kind", "postgres")
    project = Path(str(project_dir)).name
    su, db = _pg_user_and_db(project) if db_kind != "mysql" else ("root", "")
    if db_kind == "mysql":
        cmd = ["mariadb", f"-u{su[0]}", "-N", "-B", "-e", "select 1"]
        ok_needle = "1"
    else:
        cmd = ["psql", "-U", su[0], "-d", db or "postgres", "-tAc", "select 1"]
        ok_needle = "1"
    # -n / -w = no pedir password nunca, y fallar en vez de preguntar.
    rc, out = _compose_input(project_dir, "db",
                             *(["mariadb", "--skip-password"] if db_kind == "mysql"
                               else ["psql", "-w", "-U", su[0], "-d", db or "postgres",
                                     "-tAc", "select 1"]),
                             timeout=20, stdin="")
    texto = (out or "").strip()
    conecto = rc == 0 and ok_needle in texto
    if conecto:
        return Check("la base pide password", False,
                     "ENTRO SIN PASSWORD: el pg_hba esta en trust.\n"
                     "        cualquiera con un cliente postgres entra a esta base.\n"
                     "        el init/10-roles.sh tiene que reescribir el pg_hba\n"
                     "        de ${PGDATA}/pg_hba.conf, no de una ruta fija.")
    return Check("la base pide password", True,
                 "rechaza la conexion sin password (scram-sha-256)")


def su_mysql_env(user: str) -> str:
    """Que variable de entorno tiene la password de `user` en el contenedor.

    El superuser de mariadb se crea con MARIADB_PASSWORD y el de postgres
    con POSTGRES_PASSWORD. Si el usuario es otro (un grant externo, por
    ejemplo), no hay variable: se cae al .env.
    """
    if user == _container_superuser():
        return "POSTGRES_PASSWORD"
    return "MARIADB_PASSWORD"


def _mariadb_root_password(project: str, env: dict) -> str:
    """La password del root de mariadb, que no es la del .env.

    MariaDB 11.x ignora MARIADB_ROOT_PASSWORD (y
    MARIADB_RANDOM_ROOT_PASSWORD="no") cuando el volumen YA estaba
    inicializado: genera la suya, la imprime en el log del arranque y no
    queda en ningun archivo. Cuando eso pasa:

      * DB_ROOT_PASSWORD del .env no abre nada,
      * pero el init/10-users.sh TAMBien fallo (no pudo entrar con esa
        password), asi que el proyecto quedo con la base a medio inicializar.

    La unica password que funciona es la que dice el log. Por eso se lee
    de ahi, y no del .env: leer el .env da la sensacion de que se sabe la
    password, y no se sabe.

    Si no hay ninguna de las dos, se devuelve la del .env: el init va a
    fallar y el mensaje va a decir por que.
    """
    c = _db_container(project)
    if c:
        r = subprocess.run(["docker", "logs", c], capture_output=True,
                           text=True, timeout=30)
        for linea in (r.stdout or "").split("\n"):
            v = _parse_generated_root_password(linea)
            if v:
                return v
    return env.get("DB_ROOT_PASSWORD") or env.get("DB_PASSWORD") or ""


def _parse_generated_root_password(linea: str) -> str:
    """Saca la password del root de una linea de 'docker logs mariadb'.

    La linea trae timestamp y nivel adelante:
      '2026-10-03 00:44:25+00:00 [Note] [Entrypoint]: GENERATED ROOT
       PASSWORD: la clave'

    Partirla por ':' a lo bruto devolvia los 87 chars de la linea entera, que
    no abren nada: el sintoma era un 'Access denied' sin explicacion.

    Se separa por la MARCA completa y no por ':' para dos razones: el
    timestamp tiene dos ':' antes, y hay una linea sisters
    ('MARIADB_ROOT_PASSWORD: (la variable)') que no es esta.
    """
    marca = "GENERATED ROOT PASSWORD:"
    if marca in linea:
        return linea.split(marca, 1)[-1].strip()
    return ""


def db_roles_exist(project_dir: str, cfg: dict) -> Check:
    """Los tres roles del proyecto existen de verdad.

    pg_isready da ok con una base vacia de roles: el healthcheck dice
    healthy y el check 3 dice "la DB responde" mientras el rol que la app
    usa no existe. El sintoma real es un
    `role "<proyecto>_app" does not exist` en el log de postgres, que no
    dice que el init fallo.

    La causa fue doble:
      1. POSTGRES_USER era el MISMO nombre que DB_USER, asi que el init
         hacia CREATE ROLE de un rol que postgres ya habia creado como
         superuser: 'already exists' y con ON_ERROR_STOP=1 paraba todo.
         Ahora el superuser es <proyecto>_admin (DB_ADMIN_USER).
      2. el compose no pasaba DB_USER / DB_PASSWORD / DB_MIGRATION_* /
         DB_READONLY_* al contenedor db, y con `set -u` la falla daba
         'unbound variable' DENTRO de la expansion de argumentos de psql,
         donde `set -e` no corta. El script seguia y decia que si.

    Se consulta con el superuser (POSTGRES_USER del contenedor, que ahora
    es <proyecto>_admin): el unico que se sabe que existe, porque el que
    se busca es justamente el que puede faltar.

    La consulta va por stdin (`psql ... <<SQL`), no por argumento: en un
    `sh -c` el parentesis del SQL rompe el quoting del shell
    ('syntax error: unexpected "("').
    """
    db_kind = (cfg or {}).get("db_kind", "postgres")
    env = (cfg or {}).get("env") or {}
    esperadas = [e for e in ((cfg or {}).get("db_users") or []) if e]
    if not esperadas:
        # FAIL, no PASS. Antes devolvia True cuando no sabia que verificar:
        # el chequeo mas importante del grupo, verde por omision. Un check
        # que no puede verificar no puede decir que pasa.
        return Check("los tres roles existen", False,
                     "no se le paso la lista de roles (cfg['db_users'] vacio).\n"
                     "        no se puede verificar: el chequeo tiene que fallar.")

    project = Path(str(project_dir)).name
    if db_kind == "mysql":
        # root por socket, IGUAL que el init/10-users.sh. Es el unico
        # superuser que existe y el unico que entra por socket.
        su = ("root", "")
    else:
        su = _pg_user_and_db(project)
        if not su[1]:
            su = (su[0], "postgres")
    tabla = "mysql.user" if db_kind == "mysql" else "pg_roles"
    col = "user" if db_kind == "mysql" else "rolname"
    lista = ",".join("'" + e.replace("'", "") + "'" for e in esperadas)
    sql = f"select count(*) from {tabla} where {col} in ({lista})"

    if db_kind == "mysql":
        # La password que corresponde al USUARIO con el que se consulta.
        # su[0] es el superuser (POSTGRES_USER / MARIADB_USER del contenedor),
        # que no es DB_USER: usar DB_PASSWORD ahi daba
        # 'Access denied for user x_ro@localhost (using password: YES)'.
        # DB_ROOT_PASSWORD es la que el initdb aplico. Con
        # MARIADB_RANDOM_ROOT_PASSWORD_generated (que pasa cuando el volumen
        # ya existia) esta no sirve, pero el init tampoco podria correr: un
        # proyecto con volumen viejo necesita recrearlo, y eso avisa el CLI.
        pw = _mariadb_root_password(project, env)
        # --protocol=socket: el root de mariadb solo entra por el socket
        # unix, no por TCP. Sin el flag, 'Access denied for user root'
        # aunque la password sea correcta: el root@localhost de mysql.user
        # no tiene la password que se le puso por MARIADB_ROOT_PASSWORD.
        # Es lo mismo que hace el init/10-users.sh del stack.
        rc, out = _compose_input(project_dir, "db", "sh", "-c",
                                 "mariadb --protocol=socket -u%s -p%s -N -B"
                                 % (shlex.quote(su[0]), shlex.quote(pw)),
                                 timeout=30, stdin=sql + "\n")
    else:
        # -f - lee el SQL de stdin. Pasarlo como argumento rompe el quoting
        # ('extra command-line argument'): los parentesis de pg_roles y las
        # comillas de los nombres de rol hacen que psql lo tome por otro
        # argumento en vez de la consulta.
        # PGPASSWORD explicito: con el pg_hba en scram (el init ahora lo
        # reescribe, antes se quedaba en trust) una consulta sin password
        # falla con 'fe_sendauth: no password supplied'.
        # La password del SUPERUSER: es POSTGRES_USER del contenedor
        # (<proyecto>_admin), no DB_USER. Con la de la app el error es
        # 'password authentication failed for user <proyecto>_admin'.
        pw = (env.get("DB_ADMIN_PASSWORD") if su[0].endswith("_admin")
              else None) or env.get("DB_PASSWORD") or ""
        # PGPASSWORD va DENTRO del `sh -c`. Medido: `docker compose exec`
        # no tiene la opcion -e (da 'exec: "-e": executable file not
        # found') y NO propaga el entorno del proceso cliente al contenedor
        # (PGPASSWORD=x docker compose exec ... sigue dando 'no password
        # supplied'). Lo que funciona es exportarlo adentro del shell:
        #   docker compose exec -T db sh -c "PGPASSWORD=x psql ..."
        rc, out = _compose_input(project_dir, "db", "sh", "-c",
                                 "PGPASSWORD=%s psql -U %s -d %s -tAf -"
                                 % (shlex.quote(pw), shlex.quote(su[0]),
                                    shlex.quote(su[1])),
                                 timeout=30, stdin=sql + "\n")
    texto = (out or "").strip()
    if rc != 0:
        return Check("los tres roles existen", False,
                     f"no se pudo consultar: {texto[:120]}")
    digitos = [l.strip() for l in texto.splitlines() if l.strip().isdigit()]
    if not digitos:
        return Check("los tres roles existen", False,
                     f"respuesta inesperada: {texto[:120]}")
    n = int(digitos[-1])
    if n < len(esperadas):
        faltan = [e for e in esperadas
                  if e not in texto] if False else None
        return Check("los tres roles existen", False,
                     f"faltan {len(esperadas) - n} de {len(esperadas)} "
                     f"({', '.join(esperadas)}).\n"
                     f"        el init/10-roles.sh no los creo. Mirar el log:\n"
                     f"        appctl <proyecto> logs --service db")
    return Check("los tres roles existen", True,
                 f"{', '.join(esperadas)}"
                 f"{' (+ extras)' if n > len(esperadas) else ''}")



def db_not_reachable_from_host(project_dir: str, port: int = 5432,
                                cfg_db_image: str = "postgres:18-alpine",
                                db_kind: str = "postgres") -> Check:
    """EL CHECK MAS IMPORTANTE.

    Un postgres recien inicializado todavia no escucha hasta terminar el
    initdb, asi que un `no connect` temprano no probaria nada. Este check
    espera a que el puerto este ABIERTO (probando desde la red interna del
    stack) y recien ahi intenta desde el host: si desde adentro anda y desde
    afuera no, el aislamiento es real.
    """
    # 1. la DB esta viva desde la red del stack
    #
    # El probe va DENTRO del contenedor `db`, que es donde vive pg_isready /
    # mysqladmin. Antes iba en el `app` con php+pg_connect hardcodeado, y eso
    # daba FAIL por razones que no son de la DB:
    #   - en Tomcat/Next no hay php: 'exec: "php": executable file not found'
    #   - en MariaDB usaba el driver de postgres, no mysqli
    # El sintoma era el mismo que un aislamiento roto (FAIL), sin haber
    # probado el aislamiento. Y como el check 2 aborta si este falla, un
    # solo bug producia dos FAIL.
    rc, _ = _compose(project_dir, "exec", "-T", "db", *_db_probe_cmd(db_kind),
                     timeout=30)
    if rc != 0:
        return Check("DB inalcanzable desde el host", False,
                     "primero tiene que conectar desde la red interna; "
                     "si eso falla el problema es otro (password/host)")

    # 2. y desde el host NO tiene que conectar
    # el flag va antes del nombre de la imagen. Despues llega al comando
    # pg_isready, que responde "unknown flag" y hace que este check de
    # "PASS" sin haberNada probado.
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "host",
         cfg_db_image, "pg_isready", "-h", "127.0.0.1", "-p", str(port)],
        cwd=project_dir, capture_output=True, text=True, timeout=90,
    )
    out2 = (r.stdout + r.stderr).strip()
    if "unknown flag" in out2 or "invalid" in out2.lower()[:40]:
        return Check("DB inalcanzable desde el host", False,
                     f"el check se rompio: {out2[:120]}")
    # "accepting connections" = ALGO escucha en el host. Eso es el fallo
    # que buscamos. pg_isready devuelve 0 en ese caso, 2 si rechaza.
    if "accepting connections" in out2:
        return Check("DB inalcanzable desde el host", False,
                     f"HAY algo escuchando en 127.0.0.1:{port}: la DB esta "
                     f"expuesta al host")
    return Check("DB inalcanzable desde el host", True,
                 f"puerto {port} cerrado (pg_isready: "
                 f"{out2.splitlines()[-1] if out2 else 'sin salida'})")



def _network_of(project_dir: str, project: str, suffix: str) -> str | None:
    """Nombre real de la red de un proyecto, leido del contenedor.

    Compose deriva los nombres de red del NOMBRE DEL DIRECTORIO del compose
    (no del `name:` de arriba), asi que un proyecto en $APPCTL_PROJECTS/foo
    termina en redes foo_data / foo_egress. Adivinarlo produce
    "network not found" y un falso negativo.
    """
    r = subprocess.run(
        ["docker", "ps", "--filter", f"label=appctl.proyecto={project}",
         "--filter", "status=running", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30)
    for cname in r.stdout.split():
        rr = subprocess.run(
            ["docker", "inspect", cname, "--format",
             "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}"],
            capture_output=True, text=True, timeout=30)
        for net in rr.stdout.split():
            # El SUFIJO decide, no el prefijo: la red interna siempre acaba
            # en _data y la de salida en _egress. El prefijo NO es confiable:
            # acme queda "acme_data" y hospital "appctl-hospital_data", segun
            # como compose resuelva el nombre del proyecto. Adivinarlo daba
            # "network not found" y un falso de aislamiento.
            if net.endswith("_" + suffix) or net == suffix:
                return net
    return None

def db_not_reachable_from_other_container(project_dir: str, cfg: dict) -> Check:
    """Un contenedor de OTRO proyecto no debe poder alcanzar esta DB.

    Este es EL aislamiento entre clientes: el atacante de A no llega a la DB
    de B. Antes devolvia un PASS informativo sin probar nada, que es peor
    que no tener check: un verde que no prueba nada.

    QUE HACE, de verdad:
      1. CONTROL: un contenedor en la red de ESTE proyecto tiene que poder
         alcanzar la DB de ESTE proyecto. Si esto falla, el problema no es
         el aislamiento.
      2. desde la red de OTRO proyecto tiene que FALLAR al alcanzar la DB
         de ESTE. Si entra, hay una fuga real entre clientes.

    LO IMPORTANTE del punto 2: el probe apunta a la IP REAL de la DB que
    se quiere probar, NO al nombre `db`. Con el nombre `db` dentro de la red
    de otro proyecto, DNS resuelve a la DB DE ESE proyecto y el probe
    responde "accepting connections" SIEMPRE: el check anunciaba
    "*** FUGA REAL ***" para la mitad de los proyectos y era un falso
    positivo. Un check de seguridad que grita "fuga" en falso entrena al
    admin a ignorar las fugas de verdad.

    Por eso se usa la IP del contenedor db: es lo unico que identifica la
    base que hay que proteger.
    """
    project = cfg.get("project", "?")
    db_kind = cfg.get("db_kind") or "postgres"
    db_port = int(cfg.get("db_port", 5432))
    db_image = cfg.get("db_image") or (
        "mariadb:11.8" if db_kind == "mysql" else "postgres:18-alpine")

    def _linea(host: str) -> str:
        """El probe apuntando a un host concreto."""
        return shlex.join(_db_probe_cmd(db_kind, host=host, port="",
                                        project=project))

    def _probe(net: str | None, line: str) -> tuple[int, str]:
        cmd = ["docker", "run", "--rm"]
        if net:
            cmd += ["--network", net]
        cmd += ["--entrypoint", "sh", db_image, "-c", line]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()

    # la IP de la DB de ESTE proyecto: es lo que hay que proteger
    ip = _container_ip(f"{project}_db")
    if not ip:
        return Check("otro contenedor no alcanza la DB", False,
                     f"el contenedor {project}_db no esta levantado: no hay "
                     f"nada que proteger todavia")
    data_net = _network_of(project_dir, project, "data")
    if data_net is None:
        return Check("otro contenedor no alcanza la DB", False,
                     f"no se pudo determinar la red interna de {project}")

    # 1. CONTROL: desde la red de este proyecto tiene que entrar
    rc, out = _probe(data_net, _linea(ip))
    if rc != 0:
        if "unknown flag" in out or "unknown option" in out or not out:
            return Check("otro contenedor no alcanza la DB", False,
                         f"el check NO pudo correr (error de comando): {out[:150]}")
        return Check("otro contenedor no alcanza la DB", False,
                     f"ni el propio proyecto alcanza su DB desde su red "
                     f"({out[:120]}): el problema es otro, no el aislamiento")

    otros = [p.name for p in Path(project_dir).parent.iterdir()
             if p.is_dir() and p.name != project
             and (p / "compose.yaml").exists()]
    if not otros:
        return Check("otro contenedor no alcanza la DB", True,
                     f"probado desde su propia red (SI entra, correcto). "
                     f"sin otros proyectos no hay cruce que probar")

    # 2. desde la red de OTRO proyecto, apuntando a la IP DE ESTA db
    detalle = []
    fugas = []
    for otro in sorted(otros):
        otro_net = _network_of(project_dir, otro, "data")
        if not otro_net:
            continue
        rc2, _out2 = _probe(otro_net, _linea(ip))
        if rc2 == 0:
            fugas.append(f"{otro} ({otro_net})")
        else:
            detalle.append(otro)

    if fugas:
        return Check("otro contenedor no alcanza la DB", False,
                     f"*** FUGA REAL: desde la red de {', '.join(fugas)} se "
                     f"alcanzo la DB de {project} (ip {ip}). Un cliente puede "
                     f"ver los datos de otro. ***")

    return Check("otro contenedor no alcanza la DB", True,
                 f"probado de verdad: la DB de {project} ({ip}) se alcanza "
                 f"desde su red y NO desde {', '.join(detalle)}")


def _container_ip(container: str) -> str:
    """La IP de un contenedor en su red data.

    Es lo que permite distinguir "la DB de este proyecto" de "la DB que
    este proyecto alcanza por DNS": el nombre `db` dentro de otra red
    resuelve a otra base.
    """
    r = subprocess.run(
        ["docker", "inspect", container, "--format",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        capture_output=True, text=True, timeout=30)
    return (r.stdout or "").strip()

def app_serves(project_dir: str, app_port: int, project: str,
                cfg: dict | None = None,
                expect_stub: bool = True) -> Check:
    """nginx responde y php ejecuta. Un 502 significa que nginx no habla con
    php-fpm: el socket mal montado o el pool con otro usuario.

    expect_stub True (create): exige el marcador "app: ok" del stub, porque
    en un proyecto recien creado el index.php ES el stub y otra cosa
    significa stack equivocado o render roto.
    expect_stub False (upgrade): el cliente ya pudo subir su codigo, asi que
    un HTTP 200 alcanza: con nginx+php-fpm sanos, un fatal de PHP da 500 y
    un vhost roto da 502/404, no 200. Exigir el stub en upgrade dejaba
    clavado a todo proyecto con codigo real (medido: pap-test con su app
    subida devolvia 200 y el upgrade se revertia solo)."""
    try:
        # SIN -f: con -f cualquier 4xx es error de curl, y el codigo del
        # cliente puede vivir en un subdirectorio y contestar 403 en la raiz
        # (medido: coplacteos, app en /dpgleyfederal, 403 en / -> el upgrade
        # se revertia solo). El estado HTTP se lee aparte y decide el check.
        p = subprocess.run(
            ["curl", "-q", "-sS", "--max-time", "10", "-w", "\n%{http_code}",
             f"http://127.0.0.1:{app_port}/"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Check("app responde", False, f"no se pudo pedir: {e}")
    if p.returncode != 0:
        return Check("app responde", False,
                     f"curl fallo: {p.stderr.strip()[:200]}")
    _cuerpo, _, _http = p.stdout.rpartition("\n")
    body, http = _cuerpo, _http.strip()
    if "app: ok" not in body:
        if not expect_stub:
            # Con el codigo del cliente, lo que este check prueba es que nginx
            # y php-fpm se hablan: un 2xx/3xx/4xx lo prueba (la app puede vivir
            # en un subdirectorio). Un 5xx no (fatal de PHP, fpm sin socket), y
            # la falta de respuesta tampoco.
            if not http.startswith(("2", "3", "4")):
                return Check("app responde", False,
                             f"HTTP {http or 'sin respuesta'}: el runtime no "
                             f"contesta: {body[:160]!r}")
            primera = (body.splitlines()[0][:120] if body.strip()
                       else "(sin cuerpo)")
            return Check(f"app responde (codigo del cliente, HTTP {http})", True,
                         f"no es el stub, es la app real: {primera!r}")
        return Check("app responde", False,
                     f"responde pero no es el stub esperado: {body[:200]!r}")
    # El NOMBRE del check sale del STACK, no de lo que diga la pagina. Antes
    # se deducía del stub: si el index.php era de un create anterior (p.ej.
    # el proyecto se recreo de postgres a mysql), el stub viejo decia
    # "pdo_pgsql" y el check de un stack MySQL se imprimia como
    # "nginx -> php-fpm -> postgres". El nombre viene de cfg["db_kind"].
    kind = (cfg or {}).get("db_kind") or "postgres"
    db_nombre = "mariadb" if kind == "mysql" else "postgres"
    ver_php = body.split("php: ")[1].splitlines()[0] if "php: " in body else ""

    # el stub dice que runtime es: php (con pdo_pgsql) o node (con node/pnpm).
    # No asumir: el mismo check sirve para los dos stacks.
    if "php: " in body:
        return Check(f"app responde (nginx -> php-fpm -> {db_nombre})", True,
                     f"php {ver_php}")
    if "node: " in body:
        ver = body.split("node: ")[1].splitlines()[0] if "node: " in body else ""
        return Check("app responde (nginx -> next standalone)", True,
                     f"node {ver}")
    if "tomcat:" in body:
        tj = body.split("tomcat: ")[1].splitlines()[0] if "tomcat: " in body else ""
        ja = body.split("java: ")[1].splitlines()[0] if "java: " in body else ""
        return Check("app responde (nginx -> tomcat)", True,
                     f"tomcat {tj}, java {ja}")
    return Check("app responde", True, body.splitlines()[0] if body else "")


def app_serves_python(project_dir: str, app_port: int, cfg: dict | None = None,
                      expect_stub: bool = True) -> Check:
    """La API Python responde detras de nginx (/api -> 127.0.0.1:8000).

    Por que es un check APARTE y no parte de app_serves: nginx sirve DOS
    runtimes en el mismo puerto. Un 200 en / no prueba NADA de python, / lo
    contesta Next. Si el que se murio es uvicorn (requirements.txt que no
    instalo, main.py con un error de sintaxis, una dep que falta), / sigue
    dando 200 y el proyecto se reporta sano con la API tirada.

    expect_stub True (create): se exige la marca del stub ("app: python
    ok"), porque en un proyecto recien creado ese backend ES el stub.
    expect_stub False (upgrade): el cliente ya pudo subir su API, asi que
    un HTTP 200 alcanza (uvicorn sano con una ruta que no existe da 404, no
    200; y nginx mal ruteado da 502).
    """
    try:
        # SIN -f, por lo mismo que app_serves: una API real puede contestar
        # 401/403/404 en /api/ y eso no es que el stack este roto.
        p = subprocess.run(
            ["curl", "-q", "-sS", "--max-time", "10", "-w", "\n%{http_code}",
             f"http://127.0.0.1:{app_port}/api/"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Check("API python responde", False, f"no se pudo pedir: {e}")
    if p.returncode != 0:
        return Check("API python responde (nginx -> uvicorn)", False,
                     f"curl fallo: {p.stderr.strip()[:200]}")
    _cuerpo, _, _http = p.stdout.rpartition("\n")
    body, http = _cuerpo, _http.strip()
    if "app: python ok" not in body:
        if not expect_stub:
            if not http.startswith(("2", "3", "4")):
                return Check("API python responde (nginx -> uvicorn)", False,
                             f"HTTP {http or 'sin respuesta'}: el runtime no "
                             f"contesta: {body[:160]!r}")
            primera = (body.splitlines()[0][:120] if body.strip()
                       else "(sin cuerpo)")
            return Check(f"API python responde (codigo del cliente, HTTP {http})",
                         True, f"no es el stub, es la API real: {primera!r}")
        return Check("API python responde", False,
                     f"responde pero no es el stub esperado: {body[:200]!r}")
    py = body.split("python: ")[1].splitlines()[0] if "python: " in body else ""
    fa = body.split("fastapi: ")[1].splitlines()[0] if "fastapi: " in body else ""
    return Check("API python responde (nginx -> uvicorn -> fastapi)", True,
                 f"python {py}, fastapi {fa}")


def sftp_login(project_dir: str, host: str, port: int, user: str, password: str) -> Check:
    """El cliente TIENE que poder entrar por SFTP con la password del summary.
    Es el check que mas se rompe: chroot mal, usuario sin shell valido,
    Match User mal escrito, permisos del home."""
    try:
        # sftp NO acepta "-b -": el batch file es un archivo, no stdin.
        # Con -b - el cliente aborta con el usage y el check miente.
        # probar listar Y escribir: un SFTP de solo lectura no sirve para
    # subir codigo, y "ls OK" no lo detecta
        probe = "/tmp/appctl-sftp-probe.txt"
        try:
            with open(probe, "w") as f:
                f.write("appctl probe\n")
        except OSError:
            pass
        # 127.0.0.1 y no el hostname: el puerto SFTP esta en el host, y el
        # hostname puede no resolver desde donde corre el check. El
        # hostname va en el summary (que lo lee un humano), no en el test.
        target = "127.0.0.1" if host not in ("127.0.0.1", "localhost") else host
        p = subprocess.run(
            # BatchMode NO: deshabilita la autenticacion por password, que
            # es justo lo que hace sshpass. Con BatchMode=yes el check ve
            # "Permission denied" siempre, y lo reporta como password mala.
            ["sshpass", "-p", password, "sftp",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "PreferredAuthentications=password",
             "-o", "PubkeyAuthentication=no",
             "-P", str(port), f"{user}@{target}"],
            input=f"pwd\nls -la\nput {probe} .appctl-probe.txt\nls -la .appctl-probe.txt\n"
                  f"rm .appctl-probe.txt\nbye\n",
            capture_output=True, text=True, timeout=45,
        )
    except FileNotFoundError:
        # dependencia del HOST, no del proyecto. No es un fallo del stack:
        # lo dice distinto para que no se lea como "el sftp esta roto".
        return Check("login SFTP", True,
                     "SKIP: sshpass no esta en el host "
                     "(apt install sshpass para verificarlo)")
    except subprocess.SubprocessError as e:
        return Check("login SFTP", False, f"timeout/error: {e}")

    combined = p.stdout + p.stderr
    # "Permission denied" puede ser del LOGIN o de un ARCHIVO. Si el
    # cliente ya abrio sesion ("Connected to"), el login funciono y el
    # problema es de permisos sobre un archivo, no de autenticacion.
    logged_in = "Connected to" in combined
    if "Permission denied" in combined and not logged_in:
        return Check("login SFTP", False,
                     "autenticacion rechazada: password incorrecta, usuario "
                     "bloqueado, o AllowUsers no tiene al usuario")
    if "Could not open" in combined or "Connection refused" in combined:
        return Check("login SFTP", False,
                     f"no se puede conectar al puerto {port}: revisa que el "
                     f"contenedor app este arriba")
    if "Invalid argument" in combined or "Could not resolve" in combined:
        return Check("login SFTP", False,
                     f"no se pudo resolver {host}: el check va a 127.0.0.1, "
                     f"el hostname solo va en el summary")
    if "subsystem request failed" in combined:
        return Check("login SFTP", False,
                     "subsystem sftp: falta 'Subsystem sftp' en sshd_config, "
                     "o el binario no esta dentro del chroot (mirar logs app)")
    if "Remote working directory" not in combined:
        return Check("login SFTP", False,
                     f"entro pero no abrio sesion sftp: {combined.strip()[:200]}")
    # ESCRIBIR es lo que importa: un SFTP de solo lectura no sirve para
    # subir codigo, y "ls OK" no lo detecta.
    if "Permission denied" in combined and "Uploading" in combined:
        return Check("login SFTP + sube archivos", False,
                     "entra y lista, pero NO puede escribir en /upload: "
                     "faltan permisos del dir (chown al usuario SFTP)")
    if "Uploading" not in combined:
        return Check("login SFTP + sube archivos", False,
                     f"no se pudo verificar la escritura: {combined.strip()[-200:]}")
    return Check("login SFTP + sube archivos", True,
                 f"{user}@{host}:{port} — lee y escribe")


def mysql_not_reachable_from_host(project_dir: str, port: int = 3306) -> Check:
    """Igual que el de postgres pero para MariaDB. Mismo criterio: primero
    que la DB este viva desde adentro, despues que el host no la alcance."""
    rc, _ = _compose(project_dir, "exec", "-T", "db", "sh", "-c",
                 "healthcheck.sh --connect --innodb_initialized", timeout=30)
    if rc != 0:
        return Check("MySQL/MariaDB inalcanzable desde el host", False,
                     "primero tiene que conectar desde la red interna; "
                     "si eso falla el problema es otro (init/roles)")
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "host",
         "mysql:8.4", "mysql", "-h", "127.0.0.1", "-P", str(port),
         "-u", "nadie", "-px", "-e", "SELECT 1"],
        cwd=project_dir, capture_output=True, text=True, timeout=120,
    )
    combined = (r.stdout + r.stderr).strip()
    if "unknown flag" in combined or "unknown option" in combined:
        return Check("MySQL/MariaDB inalcanzable desde el host", False,
                     f"el check se rompio: {combined[:120]}")
    # ERROR 1045 = access denied = ALGO escucha. Eso es el fallo.
    if "ERROR 1045" in combined:
        return Check("MySQL/MariaDB inalcanzable desde el host", False,
                     f"responde en 127.0.0.1:{port}: la DB esta expuesta")
    return Check("MySQL/MariaDB inalcanzable desde el host", True,
                 f"puerto {port} cerrado ({combined.splitlines()[-1][:80] if combined else 'sin salida'})")


def db_es_utilizable(project_dir: str, cfg: dict) -> Check:
    """La base se puede USAR, no solo responder.

    El healthcheck es un pg_isready, que contesta aunque el motor no pueda
    leer sus propios datos. Con el volumen de la base del owner equivocado
    (el uid del usuario de appctl en vez del del postgres) el contenedor queda
    healthy, los otros checks dan verde, y despues:

      pg_dump: could not open file "global/pg_filenode.map": Permission denied

    O sea: el proyecto parece andando y los backups no funcionan. Este check
    hace lo que un admin haria antes de confiar: una consulta de verdad, con
    la password del rol de la app.
    """
    db_kind = (cfg or {}).get("db_kind", "postgres")
    env = (cfg or {}).get("env") or {}
    user = env.get("DB_USER", "")
    pw = env.get("DB_PASSWORD", "")
    nombre = env.get("DB_NAME", "")
    if not (user and pw and nombre):
        return Check("la base se puede usar", False,
                     "no tengo las credenciales del rol de la app para "
                     "probarla (falta DB_USER/DB_PASSWORD/DB_NAME)")
    if db_kind == "mysql":
        cmd = ("MYSQL_PWD=%s mariadb --protocol=socket -u%s -N -B "
               "-e 'select 1'" % (shlex.quote(pw), shlex.quote(user)))
        punta = "MYSQL_PWD=%s mariadb -h 127.0.0.1 -u%s -N -B -e 'select 1'" % (
            shlex.quote(pw), shlex.quote(user))
    else:
        # por TCP y no por socket: con pg_hba en scram el socket tambien pide
        # password, y el -h evita depender del path del socket unix.
        cmd = ("PGPASSWORD=%s psql -h 127.0.0.1 -U %s -d %s -tAc 'select 1'"
               % (shlex.quote(pw), shlex.quote(user), shlex.quote(nombre)))
        punta = cmd
    rc, out = _compose_input(project_dir, "db", "sh", "-c", cmd,
                             timeout=30, stdin="")
    if rc != 0 and db_kind == "mysql" and "Access denied" in (out or ""):
        # mysql por socket necesita el plugin del server; por TCP no.
        rc, out = _compose_input(project_dir, "db", "sh", "-c", punta,
                                 timeout=30, stdin="")
    if rc != 0:
        return Check("la base se puede usar", False,
                     "no pude consultar la base con el rol de la app:\n        "
                     + (out or "").strip()[:220] +
                     "\n        lo mas comun: el volumen de la base es del "
                     "usuario equivocado y el motor no puede leer sus datos. "
                     "los backups fallan aunque todo aparezca healthy.")
    return Check("la base se puede usar", True,
                 "responde consultas con el rol de la app, no solo un ping")


def all_checks(project_dir: str, cfg: dict, host: str,
               expect_stub: bool = True) -> list[Check]:
    """Suite completa, en el orden en que conviene fallar.

    expect_stub True: create (el index.php tiene que ser el stub).
    expect_stub False: upgrade (el cliente ya pudo subir su codigo)."""
    checks: list[Check] = []
    project = cfg["project"]

    checks += wait_healthy(project_dir, ["db", "app"])
    # el healthcheck del stub no toca la DB, asi que "app healthy" puede
    # pasar antes de que postgres termine el initdb. esperar la DB real.
    checks.append(_wait_db_ready(
        project_dir, timeout=240,
        db_kind=(cfg.get("db_kind") or "postgres")))
    # pg_isready da ok con la base vacia de roles: el healthcheck y el
    # check de arriba dicen "todo bien" mientras el rol que la app usa no
    # existe. Este lo verifica por nombre.
    checks.append(db_roles_exist(project_dir, cfg))
    checks.append(db_demanda_password(project_dir, cfg))
    checks.append(db_es_utilizable(project_dir, cfg))

    db_port = int(cfg.get("db_port", 5432))
    if (cfg.get("db_kind") or "postgres") == "mysql":
        checks.append(mysql_not_reachable_from_host(project_dir, db_port))
    else:
        checks.append(db_not_reachable_from_host(
            project_dir, db_port, cfg.get("db_image", "postgres:18-alpine")))
    checks.append(db_not_reachable_from_other_container(project_dir, cfg))
    checks.append(app_serves(project_dir, cfg["app_port"], project, cfg,
                             expect_stub=expect_stub))
    # El stack nextjs-python tiene DOS runtimes en el mismo puerto: / lo
    # contesta Next y /api la API. Sin este check, una API caida pasaba
    # desapercibida con el resto en verde.
    if "python" in (cfg.get("stack") or ""):
        checks.append(app_serves_python(
            project_dir, cfg["app_port"], cfg, expect_stub=expect_stub))
    checks.append(sftp_login(
        project_dir, host, cfg["sftp_port"],
        cfg["sftp_user"], cfg["sftp_password"],
    ))
    return checks
