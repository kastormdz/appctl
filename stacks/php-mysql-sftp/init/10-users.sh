#!/bin/bash
# MariaDB: tres usuarios, cada uno con su password y sus grants.
#
# Diferencia clave con PostgreSQL: en MySQL/MariaDB no hay "roles". Son
# usuarios, y cada GRANT lleva un HOST. Un usuario creado sin host
# (`CREATE USER x`) solo se puede conectar por socket unix, no por TCP. Si
# uno olvida el host y grantea a 'localhost', la app que entra por la red
# recibe "Access denied" aunque la password este perfecta.
#
# Por eso TODOS los usuarios se crean con host '%', y la DB es la del
# proyecto (nunca '*'), que es el equivalente MySQL del REVOKE FROM PUBLIC.
#
# Probado contra mariadb:11.8 con un cliente MySQL 8.4 por TCP:
#   appu conecta OK / CREATE TABLE -> ERROR 1142 denied
#   migu crea OK / rou INSERT -> ERROR 1142 denied
set -euo pipefail

# Comprobar ANTES de usar las variables. Con `set -u`, una que no llega al
# contenedor no corta el script con un error claro: revienta en la expansion
# del argumento del heredoc, y como la falla no es del comando sino de la
# expansion, `set -e` no corta. El script seguia y el proyecto quedaba sin
# los usuarios de migracion y de solo lectura.
for v in MARIADB_ROOT_PASSWORD DB_USER DB_PASSWORD DB_NAME \
         DB_MIGRATION_USER DB_MIGRATION_PASSWORD \
         DB_READONLY_USER DB_READONLY_PASSWORD; do
    if [ -z "${!v:-}" ]; then
        echo "[init] ERROR: la variable $v no llego al contenedor." >&2
        echo "[init]        el compose tiene que pasarla en 'environment:'." >&2
        echo "[init]        sin esto no se crean los tres usuarios del proyecto." >&2
        exit 1
    fi
done

# root por socket: es el unico canal que root tiene garantizado. La
# entrypoint de mariadb no deja entrar a root por TCP con esta config.
# Si el root tiene una password distinta de la que dice el .env, esto
# falla con 'Access denied' y no dice por que. La causa es que el volumen
# ya estaba inicializado y mariadb genero su propia: hay que borrar el
# volumen del proyecto para que el initdb aplique DB_ROOT_PASSWORD.
if ! mariadb --protocol=socket -u root -p"${MARIADB_ROOT_PASSWORD}" \
        -e "select 1" >/dev/null 2>&1; then
    echo "[init] ERROR: no puedo entrar como root por el socket." >&2
    echo "[init]        casi siempre es que el volumen de la base ya" >&2
    echo "[init]        estaba inicializado de un intento anterior, y" >&2
    echo "[init]        mariadb genero una password de root distinta de" >&2
    echo "[init]        DB_ROOT_PASSWORD (la imprime como" >&2
    echo "[init]        'GENERATED ROOT PASSWORD' en el log)." >&2
    echo "[init]        fix: appctl <proyecto> destroy --yes (borra TODO)" >&2
    echo "[init]        y volver a crear el proyecto." >&2
    exit 1
fi

mdb() {
    mariadb --protocol=socket -u root -p"${MARIADB_ROOT_PASSWORD}" "$@"
}

mdb <<SQL
-- ---------------------------------------------------------------------
-- usuario de la aplicacion
-- SELECT/INSERT/UPDATE/DELETE sobre la base. NO CREATE ni DROP:
-- un SQL injection en la app no debe poder tirar las tablas.
CREATE USER IF NOT EXISTS '${DB_USER}'@'%'
    IDENTIFIED BY '${DB_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ${DB_NAME}.* TO '${DB_USER}'@'%';

-- ---------------------------------------------------------------------
-- usuario de migraciones: todo, incluido CREATE/DROP. Para
-- 'artisan migrate', migraciones de Django, flyway, lo que sea.
CREATE USER IF NOT EXISTS '${DB_MIGRATION_USER}'@'%'
    IDENTIFIED BY '${DB_MIGRATION_PASSWORD}';
GRANT ALL PRIVILEGES
    ON ${DB_NAME}.* TO '${DB_MIGRATION_USER}'@'%' WITH GRANT OPTION;

-- CREATE USER: para que el usuario de migraciones pueda ejecutar
-- CREATE USER, que es lo que hace "appctl <p> db grant".
--
-- Hace falta lo de arriba (WITH GRANT OPTION) Y esto. Con solo
-- WITH GRANT OPTION da 1227: 'you need (at least one of) the CREATE USER
-- privilege'. MariaDB no tiene un 'GRANT OPTION' otorgable por base:
-- 'GRANT OPTION ON db.*' se rechaza con 1064. Las dos lineas juntas estan
-- verificadas contra mariadb:11.8.
--
-- Que sea seguro aca y no en un mysql compartido: la base vive en su PROPIA
-- red (internal:true) y no comparte nada con otros clientes. El aislamiento
-- entre clientes lo da la red, no los privilegios. En un mysql con varias
-- bases en el mismo servidor esto si seria un problema, y por ahi el init
-- NO lo hace.
GRANT CREATE USER ON *.* TO '${DB_MIGRATION_USER}'@'%';

-- ---------------------------------------------------------------------
-- usuario de solo lectura: reportes, backups, un panel de soporte.
CREATE USER IF NOT EXISTS '${DB_READONLY_USER}'@'%'
    IDENTIFIED BY '${DB_READONLY_PASSWORD}';
GRANT SELECT
    ON ${DB_NAME}.* TO '${DB_READONLY_USER}'@'%';

-- ---------------------------------------------------------------------
-- authentication_string: el hash va por el plugin nativo de MariaDB, no
-- por mysql_native_password de MySQL. Si se deja el default, MySQL 8.4
-- (el cliente de la app) le dice "client does not support authentication
-- protocol" y se rompe la conexion. Este es el error clasico de
-- MariaDB + cliente MySQL moderno.
ALTER USER ${DB_USER}@'%'
    IDENTIFIED VIA mysql_native_password USING PASSWORD('${DB_PASSWORD}');
ALTER USER ${DB_MIGRATION_USER}@'%'
    IDENTIFIED VIA mysql_native_password USING PASSWORD('${DB_MIGRATION_PASSWORD}');
ALTER USER ${DB_READONLY_USER}@'%'
    IDENTIFIED VIA mysql_native_password USING PASSWORD('${DB_READONLY_PASSWORD}');

FLUSH PRIVILEGES;
SQL

echo "[init] usuarios MariaDB creados con host '%':"
echo "[init]   ${DB_USER}          SELECT/INSERT/UPDATE/DELETE"
echo "[init]   ${DB_MIGRATION_USER}  ALL PRIVILEGES + GRANT OPTION (puede crear usuarios)"
echo "[init]   ${DB_READONLY_USER}   SELECT"
