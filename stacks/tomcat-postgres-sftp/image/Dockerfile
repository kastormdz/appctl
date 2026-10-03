# Tomcat directo, sin nginx adentro.
#
# El TLS y el routing los hace un HAProxy EXTERNO a este server, asi que
# nginx solo seria un reverse proxy sin nada que proxyear: 600 MB de
# imagen y un salto extra para nada.
#
# Base: la oficial de Docker (tomcat:11-jdk25-temurin-noble). Trae Tomcat
# + JDK 25 ya resueltos. Lo unico que se agrega es openssh, para el SFTP.
#
# NO usar bitnami/tomcat: desde ago-2025 el catalogo publico es de pago
# (Bitnami Secure Images) y lo que queda gratuito esta en bitnamilegacy,
# CONGELADO y sin parches de seguridad. Un server con apps de clientes no
# puede correr imagenes sin CVE patches.

ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY="localhost,127.0.0.1"

FROM tomcat:11-jdk25-temurin-noble

# solo openssh: el SFTP. Sin nginx, sin supervisor (catalina.sh va directo).
RUN apt update && apt install -y --no-install-recommends \
      openssh-server curl ca-certificates tzdata cron \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080 22
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=6 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["/usr/local/bin/entrypoint.sh"]
