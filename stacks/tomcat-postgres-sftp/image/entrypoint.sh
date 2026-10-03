#!/bin/bash
# Tomcat directo, sin nginx: catalina.sh + sshd, dos procesos, sin supervisor.
#
# El contenedor corre como root (la oficial de tomcat viene asi). El root
# es lo que permite armar el chroot de sshd con mknod y hacer chown del
# webapps, que son operaciones que un usuario sin privilegios no puede.
# El aislamiento real es la red (internal: true).
set -euo pipefail

PROJECT="${PROJECT:?falta PROJECT}"
SFTP_USER="${SFTP_USER:?falta SFTP_USER}"
SFTP_CHROOT="/srv/sftp"
: "${SFTP_UID:=1001}"
: "${SFTP_GID:=1000}"
WEBROOT="${SFTP_CHROOT}/upload"
WEBAPPS="/usr/local/tomcat/webapps"
SFTP_UID=1001
SFTP_GID=1001

log() { echo "[entrypoint] $*"; }

# ---------------------------------------------------------------- sftp ----
# La oficial de tomcat corre como usuario "ubuntu" (uid 1000), no root.
# El bind mount de webapps trae el owner del host. Catalina necesita poder
# escribir ahi para explotar los .war, y con `set -e` un chown que falla
# por eso mata el entrypoint (restart loop).
mkdir -p "${WEBROOT}" "${WEBAPPS}" "${SFTP_CHROOT}/dev" "${SFTP_CHROOT}/tmp" \
         /var/run/sshd /usr/local/tomcat/appctl
TOMCAT_UID=$(id -u tomcat 2>/dev/null || echo 1000)
chown -R "${TOMCAT_UID}:${TOMCAT_UID}" "${WEBAPPS}" 2>/dev/null || true
chmod 755 "${WEBAPPS}" 2>/dev/null || true
chmod 1777 "${SFTP_CHROOT}/tmp"
chown root:root "${SFTP_CHROOT}"
chmod 755 "${SFTP_CHROOT}"

# usuario SFTP. Debian, no Alpine: no hay addgroup -S ni adduser -S.
if ! id "${SFTP_USER}" >/dev/null 2>&1; then
    groupadd -g "${SFTP_GID}" app 2>/dev/null || true
    useradd -u "${SFTP_UID}" -g app -d /home/sftp -s /usr/sbin/nologin \
            -M "${SFTP_USER}"
    log "usuario sftp ${SFTP_USER} creado (uid ${SFTP_UID})"
fi
mkdir -p /home/sftp/upload
chown -R ${SFTP_UID}:${SFTP_GID} /home/sftp
echo "${SFTP_USER}:${SFTP_PASSWORD}" | chpasswd
chage -E -1 -I -1 -m 0 -M 99999 "${SFTP_USER}" 2>/dev/null || true

# ownership por UID numerico: no depende del orden de creacion
chown ${SFTP_UID}:${SFTP_GID} "${WEBROOT}" 2>/dev/null || true
# 755 y NO 700: el servidor web (nginx en php/nextjs) corre como OTRO
    # usuario que el dueo del upload (el del SFTP, uid 1001), y en 700 no
    # puede ni traversing. El healthcheck daba 403 y el contenedor queda
    # unhealthy para siempre. El cliente igual no puede mkdir en /upload:
    # eso lo decide el chroot (es la raiz), no el modo del directorio.
    chmod 755 "${WEBROOT}"

# --- sshd_config: las 7 capas del chroot --------------------------------
cat > /etc/ssh/sshd_config <<EOF
Port 22
AddressFamily inet
ListenAddress 0.0.0.0
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key

# El subsystem. Sin esta linea sshd no sabe que binario responderle al
# cliente y contesta "subsystem request failed on channel 0" aunque el
# binario este en el chroot y funcione.
Subsystem sftp /usr/lib/openssh/sftp-server -f AUTH -l INFO

# internal-sftp es un WRAPPER de sshd, no un binario, y no existe dentro
# del chroot. Por eso el Subsystem de arriba apunta al binario real.
ForceCommand internal-sftp -d /upload
PermitTTY no
X11Forwarding no
AllowTcpForwarding no
AllowAgentForwarding no
PermitTunnel no
GatewayPorts no
PasswordAuthentication yes
PermitEmptyPasswords no
PubkeyAuthentication yes
AllowUsers ${SFTP_USER}
# 10, no 3: un cliente normal tiene 5 llaves en ~/.ssh y las ofrece todas
# antes de que le pidan la password. Con 3, el server corta con "Too many
# authentication failures" y nunca llega a la password de appctl.
MaxAuthTries 10
MaxSessions 4
LoginGraceTime 30
ChrootDirectory ${SFTP_CHROOT}
EOF

# --- internal-sftp + libs + devices + passwd en el chroot ---------------
# Debian: loader en /lib64/ld-linux-x86-64.so.2 y libs en
# /usr/lib/x86_64-linux-gnu. NO es el path de Alpine.
SFTP_BIN=""
for c in /usr/lib/openssh/sftp-server /usr/lib/ssh/sftp-server; do
    [ -x "$c" ] && { SFTP_BIN="$c"; break; }
done
if [ -z "${SFTP_BIN}" ]; then
    log "ERROR: no encontre sftp-server en el sistema"
    exit 1
fi
mkdir -p "${SFTP_CHROOT}$(dirname "${SFTP_BIN}")" \
         "${SFTP_CHROOT}/usr/lib/x86_64-linux-gnu" \
         "${SFTP_CHROOT}/lib64" "${SFTP_CHROOT}/lib" \
         "${SFTP_CHROOT}/usr/bin" "${SFTP_CHROOT}/etc"
cp -a "${SFTP_BIN}" "${SFTP_CHROOT}${SFTP_BIN}"
chmod 755 "${SFTP_CHROOT}${SFTP_BIN}"
ldd "${SFTP_BIN}" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u | while read -r lib; do
    [ -f "${lib}" ] || continue
    mkdir -p "${SFTP_CHROOT}$(dirname "${lib}")"
    cp -aL "${lib}" "${SFTP_CHROOT}${lib}" 2>/dev/null || true
done
for b in /bin/sh /bin/ls /bin/cat /bin/mkdir /bin/rm; do
    [ -f "${b}" ] || continue
    mkdir -p "${SFTP_CHROOT}$(dirname "${b}")"
    cp -aL "${b}" "${SFTP_CHROOT}${b}" 2>/dev/null || true
    ldd "${b}" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u | while read -r lib; do
        [ -f "${lib}" ] || continue
        mkdir -p "${SFTP_CHROOT}$(dirname "${lib}")"
        cp -aL "${lib}" "${SFTP_CHROOT}${lib}" 2>/dev/null || true
    done
done
# device nodes: sin /dev/null, internal-sftp aborta con "Couldn't open
# /dev/null" y el cliente ve "subsystem request failed".
for spec in "null c 1 3 666" "zero c 1 5 666" "random c 1 8 666" "urandom c 1 9 666"; do
    set -- ${spec}
    [ -e "${SFTP_CHROOT}/dev/$1" ] || mknod -m "$5" "${SFTP_CHROOT}/dev/$1" "$2" "$3" "$4" 2>/dev/null || true
done
# /etc/passwd: sin esto, "No user found for uid 0"
grep -E "^(root|${SFTP_USER}):" /etc/passwd > "${SFTP_CHROOT}/etc/passwd" 2>/dev/null || true
grep -E "^(root|app):" /etc/group > "${SFTP_CHROOT}/etc/group" 2>/dev/null || true
OUT=$(chroot "${SFTP_CHROOT}" "${SFTP_BIN}" 2>&1 | head -1 || true)
case "${OUT}" in
    usage:*|"")
        # Una sola vez por arranque: si el entrypoint corre en loop, este
        # mensaje se repite y tapa el error real de catalina.
        log "internal-sftp OK en el chroot" ;;
    *)
        log "ERROR: internal-sftp no arranca: ${OUT}"; exit 1 ;;
esac
[ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -A >/dev/null 2>&1 || true

# --- credenciales para la app Java ---------------------------------------
# Tomcat NO lee un .env de la app: un mismo Tomcat puede servir varias apps
# y la config va POR CONTEXTO. Se generan dos cosas: un properties para
# classpath y una plantilla de context.xml para JNDI.
cat > /usr/local/tomcat/conf/appctl-db.properties <<EOF
# Generado por appctl para el proyecto ${PROJECT}.
# Se lee del classpath: getResourceAsStream("appctl-db.properties")
db.host=db
db.port=5432
db.name=${DB_NAME}
db.user=${DB_USER}
db.password=${DB_PASSWORD}
db.migration.user=${DB_MIGRATION_USER}
db.migration.password=${DB_MIGRATION_PASSWORD}
db.readonly.user=${DB_READONLY_USER}
db.readonly.password=${DB_READONLY_PASSWORD}
EOF
chmod 600 /usr/local/tomcat/conf/appctl-db.properties
# copia en lib/ para que quede en el classloader de todas las webapps
cp /usr/local/tomcat/conf/appctl-db.properties \
   /usr/local/tomcat/lib/appctl-db.properties 2>/dev/null || true

cat > /usr/local/tomcat/conf/appctl-context-template.xml <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!-- Plantilla JNDI para ${PROJECT}. Copiala a META-INF/context.xml dentro
     de tu .war, o deployalo como conf/${PROJECT}.xml -->
<Context>
    <Resource name="jdbc/${PROJECT}"
              auth="Container" type="javax.sql.DataSource"
              driverClassName="org.postgresql.Driver"
              url="jdbc:postgresql://db:5432/${DB_NAME}"
              username="${DB_USER}" password="${DB_PASSWORD}"
              maxTotal="25" maxIdle="5" maxWaitMillis="10000"/>
    <Resource name="jdbc/${PROJECT}Mig"
              auth="Container" type="javax.sql.DataSource"
              driverClassName="org.postgresql.Driver"
              url="jdbc:postgresql://db:5432/${DB_NAME}"
              username="${DB_MIGRATION_USER}" password="${DB_MIGRATION_PASSWORD}"
              maxTotal="5" maxIdle="2" maxWaitMillis="10000"/>
</Context>
EOF
chmod 600 /usr/local/tomcat/conf/appctl-context-template.xml

# --- ROOT con index y /healthz -------------------------------------------
# El healthcheck pegaria a una app que el cliente todavia no subio y el
# contenedor quedaria unhealthy para siempre. ROOT sirve / y /healthz.
#
# /healthz como JSP: se mapea con web.xml en ROOT, sin tocar server.xml.
mkdir -p "${WEBAPPS}/ROOT/WEB-INF"
printf '<html><body>app: ok<br>tomcat: up<br>proyecto: %s<br><br>subi tu .war a /upload/ y reinicia: appctl %s restart</body></html>\n' \
    "${PROJECT}" "${PROJECT}" > "${WEBAPPS}/ROOT/index.html"

cat > "${WEBAPPS}/ROOT/healthz.jsp" <<'JSP'
<%@ page contentType="text/plain; charset=utf-8" %>ok
JSP

cat > "${WEBAPPS}/ROOT/WEB-INF/web.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<web-app version="3.1" xmlns="http://xmlns.jcp.org/xml/ns/javaee">
  <!-- Solo el healthcheck. El cliente puede sobreescribir ROOT con su
       web.xml si necesita, y entonces este mapping se pierde: en ese
       caso appctl <proyecto> restart lo vuelve a generar. -->
  <servlet>
    <servlet-name>healthz</servlet-name>
    <jsp-file>/healthz.jsp</jsp-file>
  </servlet>
  <servlet-mapping>
    <servlet-name>healthz</servlet-name>
    <url-pattern>/healthz</url-pattern>
  </servlet-mapping>
</web-app>
XML
cp /usr/local/tomcat/lib/appctl-db.properties \
   "${WEBAPPS}/ROOT/appctl-db.properties" 2>/dev/null || true

# --- tomcat-users.xml: el mismo usuario y password del SFTP -------------
# La oficial de tomcat NO incluye ningun usuario con role manager-gui: hay
# que definirlo en tomcat-users.xml. Se usa la MISMA credencial que el SFTP
# (SFTP_USER / SFTP_PASSWORD) para que el admin tenga una sola: mismo user,
# misma password, para las tres cosas (deploy, manager y archivos).
cat > /usr/local/tomcat/conf/tomcat-users.xml <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<tomcat-users xmlns="http://tomcat.apache.org/xml"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:schemaLocation="http://tomcat.apache.org/xml
              http://tomcat.apache.org/xsd/tomcat-users.xsd"
              version="1.0">
  <!-- Generado por appctl. Misma credencial que el SFTP: ${SFTP_USER} -->
  <role rolename="manager-gui"/>
  <role rolename="host-manager"/>
  <role rolename="admin-gui"/>
  <!-- tomcat: el rol que hace deployar .war por HTTP -->
  <role rolename="tomcat"/>
  <user username="${SFTP_USER}" password="${SFTP_PASSWORD}" roles="tomcat,manager-gui,host-manager,admin-gui"/>
</tomcat-users>
EOF
chmod 600 /usr/local/tomcat/conf/tomcat-users.xml
log "tomcat-users.xml: ${SFTP_USER} con roles tomcat,manager-gui,host-manager,admin-gui"

# --- el .war del cliente -------------------------------------------------
# El cliente sube el .war YA COMPILADO (mvn package / gradle build). No hay
# build en el arranque: compilar Java necesita Maven o Gradle con todas sus
# deps, que son 500 MB-1 GB de imagen y minutos POR CLIENTE.
WAR=""
if [ -f "${WEBROOT}/${PROJECT}.war" ]; then
    WAR="${PROJECT}.war"
else
    # `set -euo pipefail` + `$(ls | head | xargs)` = muerte. El pipefail
    # propaga el exit 2 del ls (directorio vacio o sin *.war) y el set -e
    # corta el entrypoint EN SILENCIO, sin log: el contenedor entra en
    # restart loop y no dice por que.
    # Por que: el ultimo comando del pipe (xargs -r) da 0, pero pipefail
    # mira todos. La forma segura: || true explicito.
    WAR=$(ls "${WEBROOT}"/*.war 2>/dev/null | head -1 | xargs -r basename || true)
    WAR="${WAR:-}"
fi
if [ -n "${WAR}" ]; then
    log "deployando ${WAR} (contexto /${WAR%.war}/)"
    cp "${WEBROOT}/${WAR}" "${WEBAPPS}/" 2>/dev/null || true
    printf 'CONTEXT=%s\n' "${WAR%.war}" > /usr/local/tomcat/appctl-context
else
    log "no hay .war todavia: subi uno a /upload/ y reinicia"
    printf 'CONTEXT=\n' > /usr/local/tomcat/appctl-context
fi

# --- arranque: sshd en background, catalina al frente -------------------
# --- cron ------------------------------------------------------------------
# Cron NO es un stack: no cambia la imagen ni la DB. Es una bandera
# (CRON_ENABLED) y el cliente sube su crontab a /upload/cron/crontab por
# SFTP, que es el mismo chroot del webroot. Sin esto, cron seria otro
# directorio de stack duplicado por cada combinacion runtime x db x sftp.
if [ "${CRON_ENABLED:-0}" = "1" ]; then
    # El directorio se crea con el owner del usuario SFTP porque el cliente
    # sube el archivo DIRECTO adentro (no puede hacer mkdir: /upload es 700,
    # y eso es correcto para que nadie mas escriba).
    CRON_DIR="${WEBROOT}/cron"
    mkdir -p "${CRON_DIR}" /var/spool/cron/crontabs /etc/cron.d
    chmod 755 /var/spool/cron/crontabs
    # El mismo chown que recibe /upload: sin esto el directorio queda
    # root:root 755, el cliente entra por SFTP pero `mkdir /upload/cron`
    # falla con "Failure" y el crontab nunca llega. El upload y el cron
    # tienen que ser del MISMO usuario.
    chown "${SFTP_UID}:${SFTP_GID}" "${CRON_DIR}" 2>/dev/null || chown "${SFTP_UID}" "${CRON_DIR}" 2>/dev/null || true
    chmod 775 "${CRON_DIR}" 2>/dev/null || true
    if [ -f "${CRON_DIR}/crontab" ]; then
        # el crontab del cliente se carga TAL CUAL: si quiere una macro de
        # ejemplo o un wrapper, los escribe el.
        crontab -u app "${CRON_DIR}/crontab" 2>/dev/null \
            || crontab "${CRON_DIR}/crontab" 2>/dev/null \
            || log "AVISO: crontab del cliente invalido; cron queda vacio"
        log "crontab cargado desde ${CRON_DIR}/crontab"
    else
        log "cron activo sin crontab: subi uno a /upload/cron/crontab"
    fi
    # CRON_TZ evita que un job de las 3am corra a otra hora: el contenedor
    # esta en UTC por defecto.
    printf 'CRON_TZ=%s\n' "${TZ:-America/Argentina/Buenos_Aires}" > /etc/cron.d/appctl-tz
fi

# --- manager y host-manager ---------------------------------------------
# La oficial de Tomcat 11 NO trae el manager desplegado: lo deja en
# /usr/local/tomcat/webapps.dist/ (manager, host-manager, docs, examples),
# que es el directorio de ejemplo. webapps/ arranca VACIO. Por eso
# /manager/html daba 404: no era el bind mount ni el tomcat-users.xml, era
# que el webapp nunca existio en ninguna parte.
#
# Se copian (no se symlink) porque Tomcat los despliega expandinglos en
# webapps/ y necesita escribir ahi.
if [ ! -d "${WEBAPPS}/manager" ] && [ -d /usr/local/tomcat/webapps.dist/manager ]; then
    cp -r /usr/local/tomcat/webapps.dist/manager "${WEBAPPS}/" 2>/dev/null || true
fi
if [ ! -d "${WEBAPPS}/host-manager" ] && [ -d /usr/local/tomcat/webapps.dist/host-manager ]; then
    cp -r /usr/local/tomcat/webapps.dist/host-manager "${WEBAPPS}/" 2>/dev/null || true
fi
if [ ! -d "${WEBAPPS}/docs" ] && [ -d /usr/local/tomcat/webapps.dist/docs ]; then
    cp -r /usr/local/tomcat/webapps.dist/docs "${WEBAPPS}/" 2>/dev/null || true
fi
log "webapps: $(ls ${WEBAPPS} 2>/dev/null | tr '\n' ' ')"

# El manager esta desplegado pero Tomcat lo BLOQUEA desde otra maquina:
# "403 Access Denied. By default the Manager is only accessible from a
# browser running on the same machine as Tomcat."
#
# En Tomcat 11 el filtro NO se llama RemoteAddrFilter: es un RemoteCIDRValve
# con allow="127.0.0.1/8,::1/128". Buscar el nombre viejo hacia creer que
# el filtro ya no estaba. Por eso el grep dice 0 y el manager sigue en 403.
#
# Se borra el valve (no se reemplaza por allow-all): el manager conserva su
# autenticacion propia (tomcat-users.xml + rol manager-gui) y su token CSRF.
# La proteccion que queda es la de credenciales, que es la que importa.
for _mgr in manager host-manager; do
    _ctx="${WEBAPPS}/${_mgr}/META-INF/context.xml"
    [ -f "${_ctx}" ] || continue
    # borrar el valve que corta el acceso remoto (RemoteCIDRValve en Tomcat 11,
    # RemoteAddrFilter en el 9/10: se contemplan los dos por si cambia)
    sed -i '/RemoteCIDRValve/,+1d' "${_ctx}" 2>/dev/null || true
    sed -i '/RemoteAddrFilter/,+1d' "${_ctx}" 2>/dev/null || true
    if grep -qE 'RemoteCIDRValve|RemoteAddrFilter' "${_ctx}" 2>/dev/null; then
        log "AVISO: el filtro de acceso remoto del ${_mgr} SIGUE en su context.xml"
    else
        log "${_mgr}: acceso remoto habilitado (filtro de IP removido)"
    fi
done


log "arrancando catalina + sshd"
/usr/sbin/sshd -D -e &
SSHD_PID=$!
# Tomcat NO usa supervisor: catalina va al frente. Si el proyecto pidio cron
# se levanta aca, en background, con el log al stdout del contenedor igual
# que sshd, y el trap lo mata con el resto.
CRON_PID=""
if [ "${CRON_ENABLED:-0}" = "1" ]; then
    cron -f -L 15 &
    CRON_PID=$!
    log "cron lanzado (pid ${CRON_PID})"
fi
trap 'kill ${SSHD_PID} ${CRON_PID} 2>/dev/null || true' TERM INT

exec /usr/local/tomcat/bin/catalina.sh run
