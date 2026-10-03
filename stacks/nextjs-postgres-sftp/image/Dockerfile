# El proxy como build arg: el repo se sincroniza a maquinas que no usan
# un proxy corporativo. Sin esto, apk/npm no llegan a sus repos.
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY="localhost,127.0.0.1"

FROM node:22-alpine

# nginx adelante + sshd para el SFTP, como en el stack de PHP.
# Next.js corre standalone (node server.js), no next start: menos overhead
# de CPU y RAM, que es lo que importa cuando van 15 clientes en un server.
RUN apk update && apk add --no-cache \
      nginx supervisor openssh-server openssh-sftp-server \
      tzdata icu-libs curl busybox-extras

# pnpm con npm, NO con corepack.
#
# corepack usa el fetch NATIVO de node, que no lee HTTP_PROXY: en la red de
# proxy corporativo da "Internal Error" al pedir registry.npmjs.org.
# npm si lee el
# proxy. Verificado: corepack falla, npm install -g pnpm da 12.8.1.
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

COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx.conf /usr/local/share/appctl/nginx.conf
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY fixperms.sh /usr/local/bin/fixperms.sh
COPY supervisord.conf /etc/supervisord.conf

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/fixperms.sh \
 && mkdir -p /srv/sftp /srv/sftp/upload /app/.next /run/nginx /var/log/nginx \
 && rm -f /etc/nginx/conf.d/default.conf

EXPOSE 80 22
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=6 \
  CMD curl -fsS http://127.0.0.1/healthz || exit 1

CMD ["/usr/local/bin/entrypoint.sh"]
