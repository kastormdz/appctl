#!/bin/bash
# Corre UNA vez, cuando ./db/data esta vacio (initdb).
#
# Crea los roles del proyecto. POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD
# ya cre la DB y su dueno con la entrypoint de postgres; aca van los roles
# extra y el pg_hba.
set -euo pipefail

# --- los tres roles del proyecto ----------------------------------------
# Tres, no uno, por una razon de seguridad concreta:
#   app  -> SELECT/INSERT/UPDATE/DELETE. NO puede crear ni dropear el schema.
#   mig  -> todo, incluido CREATE SCHEMA. Para `artisan migrate`.
#   ro   -> solo SELECT. Para reportes y backups.
# Mezclar app con mig significa que un SQL injection en la aplicacion puede
# DROP TABLE. No es teorico: es el error mas comun de un hosting.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v db_user="$DB_USER" \
    -v db_pass="$DB_PASSWORD" \
    -v mig_user="$DB_MIGRATION_USER" \
    -v mig_pass="$DB_MIGRATION_PASSWORD" \
    -v ro_user="$DB_READONLY_USER" \
    -v ro_pass="$DB_READONLY_PASSWORD" \
<<'SQL'
\echo '>> creando roles del proyecto'

-- rol de la aplicacion
CREATE ROLE :"db_user" LOGIN PASSWORD :'db_pass';
GRANT CONNECT ON DATABASE :"POSTGRES_DB" TO :"db_user";
GRANT USAGE ON SCHEMA public TO :"db_user";
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public TO :"db_user";
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public TO :"db_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"db_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"db_user";

-- rol de migraciones: CREATE/DROP si o si
CREATE ROLE :"mig_user" LOGIN PASSWORD :'mig_pass';
GRANT CONNECT ON DATABASE :"POSTGRES_DB" TO :"mig_user";
GRANT ALL ON SCHEMA public TO :"mig_user";
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO :"mig_user";
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO :"mig_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO :"mig_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO :"mig_user";

-- rol de solo lectura
CREATE ROLE :"ro_user" LOGIN PASSWORD :'ro_pass';
GRANT CONNECT ON DATABASE :"POSTGRES_DB" TO :"ro_user";
GRANT USAGE ON SCHEMA public TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO :"ro_user";

-- El dueno de la base tambien necesita poder crear objetos para que
-- `migrate` funcione con el rol mig.
GRANT ALL ON SCHEMA public TO :"mig_user";

-- public no entra a esta base. Sin esto, CUALQUIER rol del cluster
-- (incluidos los de otros proyectos) puede conectarse.
REVOKE ALL ON DATABASE :"POSTGRES_DB" FROM PUBLIC;
\echo '>> roles creados'
SQL

# --- pg_hba: scrypt y cercijarse que el trust no quedo ------------------
# La imagen oficial arranca initdb con las entradas locales en `trust`
# (host all all 127.0.0.1/32 trust). Para localhost no es grave, pero si
# alguien amplia la listen_addresses o corre un `psql` desde otro pod, esa
# linea trust es una puerta abierta sin password.
#
# Se reescribe a scram-sha-256 para todas las conexiones locales de esta
# instancia, y se fuerza el listen_addresses a la red del proyecto.
HBA=/var/lib/postgresql/data/pg_hba.conf
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
CONF=/var/lib/postgresql/data/postgresql.conf
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
