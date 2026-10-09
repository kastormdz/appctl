# El proxy como build arg: el repo se sincroniza a maquinas que no usan
# un proxy corporativo. Sin esto, apk/npm/pip no llegan a sus repos.
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY="localhost,127.0.0.1"

FROM node:22-alpine

# nginx adelante + sshd para el SFTP, como en el resto de los stacks.
# Python: el runtime de la API. Se instala del repo de Alpine en vez de
# compilar un Python propio.
#
# build-base / libffi-dev / postgresql-dev NO son de adorno: son las
# cabeceras para compilar las deps de python que no publican wheel para
# musl (pip no encuentra wheel y baja el sdist: sin gcc falla con
# "command 'gcc' failed"). Con esto, `pip install -r requirements.txt`
# compila lo que haga falta.
RUN apk update && apk add --no-cache \
      nginx supervisor openssh-server openssh-sftp-server \
      tzdata icu-libs curl busybox-extras \
      python3 py3-pip \
      build-base libffi-dev postgresql-dev

# pnpm con npm, NO con corepack.
#
# corepack usa el fetch NATIVO de node, que no lee HTTP_PROXY: en la red de
# proxy corporativo da "Internal Error" al pedir registry.npmjs.org.
# npm si lee el proxy. Verificado: corepack falla, npm install -g pnpm da 12.8.1.
RUN npm install -g pnpm@latest --registry=https://registry.npmjs.org/ \
 && pnpm --version

# El node oficial esta en /usr/local/bin/node, NO en /usr/bin/node.
# El supervisord.conf lo llama por path absoluto: hardcodear el de la
# imagen oficial lo vuelve brittle ante un cambio de upstream. Un symlink
# en /usr/bin lo hace explicito y sobrevive el path real.
RUN ln -sf "$(command -v node)" /usr/bin/node \
 && ln -sf "$(command -v npm)"  /usr/bin/npm \
 && ln -sf "$(command -v npx)"  /usr/bin/npx \
 && node --version && pnpm --version

# venv PROPIO para las deps de python.
#
# NO se instala en el python del sistema: Alpine marca su python como
# "externally managed" (PEP 668) y un `pip install` a secas muere con
# "error: externally-managed-environment". El venv ademas aisla las deps
# del cliente de las del sistema.
#
# fastapi y uvicorn van preinstalados: el stub de verificacion tiene que
# andar SIN red (un create recien hecho no deberia depender de pypi), y el
# 90% de los proyectos usa justo esto.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir fastapi "uvicorn[standard]" \
 && /opt/venv/bin/python -c "import sys, fastapi, uvicorn; \
      print('python', sys.version.split()[0], '| fastapi', fastapi.__version__, \
            '| uvicorn', uvicorn.__version__)"

COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx.conf /usr/local/share/appctl/nginx.conf
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY python-launch.sh /usr/local/bin/python-launch.sh
COPY supervisord.conf /etc/supervisord.conf

RUN chmod +x /usr/local/bin/entrypoint.sh \
      /usr/local/bin/python-launch.sh \
 && mkdir -p /srv/sftp /srv/sftp/upload /srv/sftp/private \
      /app/.next /app/backend /run/nginx /var/log/nginx \
 && rm -f /etc/nginx/conf.d/default.conf

EXPOSE 80 22
# El healthcheck cubre LOS DOS runtimes: con la API caida pero Next andando,
# un check que solo mira /healthz deja el contenedor en verde y el proyecto
# se reporta sano con media aplicacion muerta.
# start-period generoso: el primer arranque instala node_modules y
# requirements.txt (si el cliente los subio sin node_modules/ ni wheels).
# La API se pide DIRECTO a 127.0.0.1:8000 (es interna; /api lo sirve
# Next). El de aca es el fallback de la imagen; el del compose manda.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=6 \
  CMD curl -qfsS http://127.0.0.1/healthz >/dev/null \
      && curl -qfsS http://127.0.0.1:8000/healthz >/dev/null || exit 1

CMD ["/usr/local/bin/entrypoint.sh"]
