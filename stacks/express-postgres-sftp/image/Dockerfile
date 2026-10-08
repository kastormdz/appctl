# El proxy como build arg: el repo se sincroniza a maquinas que no usan
# un proxy corporativo. Sin esto, apk/npm no llegan a sus repos.
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY="localhost,127.0.0.1"

FROM node:22-alpine

# nginx adelante + sshd para el SFTP, como en los otros stacks.
# node:22-alpine ya trae el runtime; nginx sirve el build de React y proxya
# /api al backend de Express.
RUN apk update && apk add --no-cache \
      nginx supervisor openssh-server openssh-sftp-server \
      tzdata icu-libs curl busybox-extras

# pnpm con npm, NO con corepack: corepack usa el fetch NATIVO de node, que no
# lee HTTP_PROXY y en la red de proxy corporativo da "Internal Error".
RUN npm install -g pnpm@latest --registry=https://registry.npmjs.org/ \
 && pnpm --version

# El node oficial esta en /usr/local/bin/node, NO en /usr/bin/node.
RUN ln -sf "$(command -v node)" /usr/bin/node \
 && ln -sf "$(command -v npm)"  /usr/bin/npm \
 && ln -sf "$(command -v npx)"  /usr/bin/npx \
 && node --version && pnpm --version

# El stub del backend ES Express, instalado en el BUILD (con el proxy) y no en
# el arranque: si dependiera de `npm install` en runtime, un proyecto sin app
# propia quedaria sin API por un problema de red, y el stub existe justamente
# para poder verificar el stack recien creado. /opt/appctl/stub tiene su propio
# node_modules, asi que `require('express')` resuelve ahi sin tocar /app.
RUN mkdir -p /opt/appctl/stub \
 && cd /opt/appctl/stub \
 && npm init -y >/dev/null \
 && npm install express@4 --omit=dev --registry=https://registry.npmjs.org/ >/dev/null \
 && node -e "require('express'); console.log('express del stub OK')"

# El stub: se copia DESPUES del install para que npm no lo pise.
COPY stub-server.js /opt/appctl/stub/server.js

COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx.conf /usr/local/share/appctl/nginx.conf
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY fixperms.sh /usr/local/bin/fixperms.sh
COPY supervisord.conf /etc/supervisord.conf

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/fixperms.sh \
 && mkdir -p /srv/sftp /srv/sftp/upload /app/backend /app/frontend-dist \
             /run/nginx /var/log/nginx \
 && rm -f /etc/nginx/conf.d/default.conf

EXPOSE 80 22

# Healthcheck del STACK, no de la app: nginx contestando CUALQUIER codigo
# (1xx-4xx) prueba que el stack esta arriba. Atarlo a /healthz de la app (como
# en los otros stacks) marca unhealthy para siempre a todo cliente cuyo codigo
# no sirva ese archivo — pasa en cuanto suben su app de verdad. Un 5xx o el
# silencio si son fallas reales del runtime.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=6 \
  CMD curl -q -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/ \
      | grep -qE '^[1-4]' || exit 1

CMD ["/usr/local/bin/entrypoint.sh"]
