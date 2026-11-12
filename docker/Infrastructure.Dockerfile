# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
# Один небольшой слой поверх неизменённых официальных Alpine-образов.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/proxy_ca > /tmp/build-ca.pem; \
        export SSL_CERT_FILE=/tmp/build-ca.pem; \
    fi; \
    apk add --no-cache iptables util-linux && rm -f /tmp/build-ca.pem

COPY --chmod=755 docker/firewall-entrypoint.sh /usr/local/bin/seps-entrypoint
ENTRYPOINT ["/usr/local/bin/seps-entrypoint"]
