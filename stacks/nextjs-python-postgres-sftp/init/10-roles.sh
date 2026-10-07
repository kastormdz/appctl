#!/bin/bash
# Corre UNA vez, cuando ./db/data esta vacio (initdb).
#
# Crea los tres roles del proyecto (app / migracion / readonly) y deja el
# pg_hba en scram-sha-256. POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD ya
# crean la DB y su dueno con la entrypoint de postgres.
# SIN `set -e` en la parte de SQL: un error de postgres en un GRANT hace que
# la entrypoint termine el contenedor, y queda sin base. Un init a medias con
# el log diciendo que fallo es mejor que un servicio caido.
set -u -o pipefail

# Comprobar ANTES de usar las variables. Con `set -u`, una variable que no
# llega al contenedor no revienta el script con un error claro: revienta en
# la linea del psql, dentro de la expansion de argumentos, y como la falla
# no es del comando sino de la expansion, `set -e` no corta. El script
# seguia, imprimia "roles creados" y los tres roles no existian.
for v in POSTGRES_USER POSTGRES_DB DB_USER DB_PASSWORD \
         DB_MIGRATION_USER DB_MIGRATION_PASSWORD \
         DB_READONLY_USER DB_READONLY_PASSWORD; do
    if [ -z "${!v:-}" ]; then
        echo "[init] ERROR: la variable $v no llego al contenedor." >&2
        echo "[init]        el compose tiene que pasarla en 'environment:'." >&2
        echo "[init]        sin esto los tres roles del proyecto no se crean." >&2
        exit 1
    fi
done

# --- los tres roles del proyecto -----------------------------------------
# Tres, no uno, por una razon de seguridad concreta:
#   app  -> SELECT/INSERT/UPDATE/DELETE. NO puede crear ni dropear el schema.
#   mig  -> todo, incluido CREATE SCHEMA. Para `artisan migrate`.
#   ro   -> solo SELECT. Para reportes y backups.
# Mezclar app con mig significa que un SQL injection en la aplicacion puede
# DROP TABLE. No es teorico: es el error mas comun de un hosting.
#
# POR QUE NO HAY UN `DO $$` CON CREATE ROLE
#
# Las variables de psql (:"db_user", :'db_user') NO se expanden adentro de
# un cuerpo dollar-quoted: el dollar-quoting desactiva la interpolacion y
# postgres recibe el ':' literal ('syntax error at or near ":"'). Se
# probaron cinco formas y las cinco fallaron:
#   1. CREATE ROLE :"db_user"                -> syntax error at or near ":"
#   2. lo mismo dentro de DO $$ ... END $$   -> idem
#   3. EXECUTE format(..., :'db_user')       -> idem, el ':' sigue llegando
#   4. SELECT ... \gset para llevarlos dentro  -> invalid variable name
#      (\gset sin alias toma los nombres de columna) y el ':' igual
#   5. getenv()                              -> no existe en postgres
# La que SI funciona es un solo format() de psql, sin DO: arma la sentencia
# completa con el nombre ya puesto y con el quoting de %I/%L, y \gexec la
# ejecuta. La idempotencia la hace el script con una consulta previa, que
# ademas deja un mensaje legible en vez de un error de postgres.
psql_run() {
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -tA "$@"
}

rol_existe() {
    psql_run -c "select 1 from pg_roles where rolname = '$1'" 2>/dev/null | grep -q 1
}

# crear_rol <usuario> <password> <etiqueta>
crear_rol() {
    local u="$1" p="$2" etiqueta="$3"
    if rol_existe "$u"; then
        echo "[init] el rol $u ya existe: se actualiza la password"
        psql_run <<SQL
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', '$u', '$p')
\gexec
SQL
    else
        echo "[init] creando el rol $u ($etiqueta)"
        psql_run <<SQL
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '$u', '$p')
\gexec
SQL
    fi
}

echo ">> creando roles del proyecto"
crear_rol "$DB_USER" "$DB_PASSWORD" "app"
crear_rol "$DB_MIGRATION_USER" "$DB_MIGRATION_PASSWORD" "migracion"
crear_rol "$DB_READONLY_USER" "$DB_READONLY_PASSWORD" "readonly"

# Los GRANT. Este bloque es SQL normal, sin dollar-quoting, asi que las
# variables de psql SI se expanden: :"db_user" y :'db_pass' funcionan bien.
#
# EL ORDEN IMPORTA, y no es cosmetico:
#   1. REVOKE de PUBLIC. Sin esto, CUALQUIER rol del cluster (incluidos los
#      de otros proyectos, porque cada proyecto es su propio postgres pero
#      los defaults del cluster se comparten) entra a esta base.
#   2. Los GRANT de cada rol. El REVOKE de arriba tambien se lleva lo que
#      public tenia sobre el schema public, asi que si los GRANT van antes
#      el rol app queda sin USAGE: 'permission denied for schema public'.
#   3. CREATE sobre la BASE, no solo sobre el schema. En postgres, CREATE
#      TABLE dentro de public necesita CREATE en el schema, pero crear un
#      SCHEMA nuevo (que es lo que hace `artisan migrate` y lo que necesita
#      el rol de migracion) necesita CREATE sobre la BASE.
psql_run -v POSTGRES_DB="$POSTGRES_DB" \
         -v db_user="$DB_USER" \
         -v mig_user="$DB_MIGRATION_USER" \
         -v ro_user="$DB_READONLY_USER" <<'SQL'
\echo '>> GRANT a los roles del proyecto'

REVOKE ALL ON DATABASE :"POSTGRES_DB" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- rol de la aplicacion: DML sobre las tablas, nunca DROP de las de otros.
-- CREATE sobre la base y sobre public porque una app con migraciones
-- propias (laravel, django) crea tablas al arrancar.
GRANT CONNECT, CREATE ON DATABASE :"POSTGRES_DB" TO :"db_user", :"mig_user";
-- ro solo entra: sin CREATE no puede crear un schema.
GRANT CONNECT ON DATABASE :"POSTGRES_DB" TO :"ro_user";
GRANT USAGE, CREATE ON SCHEMA public TO :"db_user", :"mig_user";
GRANT USAGE ON SCHEMA public TO :"ro_user";
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public TO :"db_user";
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public TO :"db_user";

-- rol de migraciones: todo, incluido CREATE SCHEMA (por eso el CREATE de
-- arriba sobre la base) y DROP sobre cualquier objeto.
GRANT ALL ON SCHEMA public TO :"mig_user";
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO :"mig_user";
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO :"mig_user";

-- rol de solo lectura
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_user";

-- Los ALTER DEFAULT PRIVILEGES tienen que ejecutarse COMO EL ROL QUE VA A
-- CREAR LAS TABLAS, no como el superuser: postgres guarda los default
-- privileges con el par (rol que los creo, rol que los recibe). Si los
-- pido como admin, quedan (admin, ro) y las tablas que cree despues el rol
-- app no heredan nada: el readonly no las ve.

\echo '>> roles creados'
SQL

# Default privileges, cada uno como el rol que crea objetos. Se hace con
# SET ROLE para no tener que conectar con la password de cada rol: el
# superuser del proyecto puede impersonarlos sin saberla.
psql_run <<SQL
SET ROLE "$DB_USER";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "$DB_USER";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO "$DB_READONLY_USER";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO "$DB_USER";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO "$DB_READONLY_USER";
SQL

psql_run <<SQL
SET ROLE "$DB_MIGRATION_USER";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO "$DB_MIGRATION_USER";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO "$DB_READONLY_USER";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO "$DB_MIGRATION_USER";
SQL

# --- pg_hba: scrypt y cercijarse que el trust no quedo ------------------
# La imagen oficial arranca initdb con las entradas locales en `trust`
# (host all all 127.0.0.1/32 trust). Para localhost no es grave, pero si
# alguien amplia la listen_addresses o corre un `psql` desde otro pod, esa
# linea trust es una puerta abierta sin password.
#
# Se reescribe a scram-sha-256 para todas las conexiones locales de esta
# instancia, y se fuerza el listen_addresses a la red del proyecto.
# La ruta tiene que salir de $PGDATA. Acertar la de memoria
# (/var/lib/postgresql/data/pg_hba.conf) NO funciona cuando el compose define
# PGDATA=/var/lib/postgresql/data/pgdata: el archivo real esta un nivel mas
# adentro, el `cp` fallaba con "No such file" y el pg_hba NUNCA se
# reescribio. Con trust hacia adentro, cualquier rol del cluster (incluidos
# los de otros proyectos) entra a esta base sin password.
DATA="${PGDATA:-/var/lib/postgresql/data/pgdata}"
HBA="$DATA/pg_hba.conf"
if [ ! -f "$HBA" ]; then
    echo "[init] ERROR: no encuentro pg_hba.conf en $HBA" >&2
    echo "[init]        PGDATA=$PGDATA" >&2
    exit 1
fi
cp -a "$HBA" "$HBA.orig"

cat > "$HBA" <<'HBA'
# appctl: generado en la creacion del proyecto. scram-sha-256, no trust.
# TYPE  DATABASE        USER            ADDRESS                 METHOD

# local (socket unix del contenedor)
local   all             all                                     scram-sha-256
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256

# la red bridge del proyecto, que es internal (sin gateway)
host    all             all             172.16.0.0/12           scram-sha-256
host    all             all             192.168.0.0/16          scram-sha-256

# replicacion, solo local
local   replication     all                                     scram-sha-256
host    replication     all             127.0.0.1/32            scram-sha-256
HBA

# escucha solo en la red del stack, nunca en 0.0.0.0 exposed
CONF="$DATA/postgresql.conf"
if ! grep -q "^# appctl" "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'CONF'

# --- appctl ------------------------------------------------------------
# La DB no se publica ningun puerto (red internal:true sin gateway).
# Aun asi, que no escuche en 0.0.0.0: si alguien publica el puerto a mano
# por error, no hay nada escuchando en la interfaz del host.
listen_addresses = '*'
# scram es el metodo de autenticacion, no md5. md5 esta deprecado.
password_encryption = 'scram-sha-256'
# un cliente que se cuelga no se lleva los 100 slots de conexiones
idle_in_transaction_session_timeout = '60s'
statement_timeout = '0'
log_connections = on
log_disconnections = on
CONF
fi

echo "[init] pg_hba.conf reescrito a scram-sha-256 (backup en pg_hba.conf.orig)"
echo "[init] roles: ${DB_USER}, ${DB_MIGRATION_USER}, ${DB_READONLY_USER}"
