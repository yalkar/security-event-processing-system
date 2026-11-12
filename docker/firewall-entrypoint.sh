#!/bin/sh
set -eu

# Только filter-таблица текущего network namespace контейнера.
# NAT-таблица Docker (в том числе встроенный DNS) остаётся нетронутой.
prefix=${SEPS_NETWORK_PREFIX:-172.28}
role=${SEPS_ROLE:?SEPS_ROLE is required}
rules=$(mktemp)
trap 'rm -f "$rules"' EXIT

cat > "$rules" <<'RULES'
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
-A OUTPUT -o lo -j ACCEPT
-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
RULES

allow_out() {
    printf '%s\n' "-A OUTPUT -s $prefix.$1 -d $prefix.$2 -p tcp --dport $3 -m conntrack --ctstate NEW -j ACCEPT" >> "$rules"
}

allow_in() {
    printf '%s\n' "-A INPUT -s $prefix.$1 -d $prefix.$2 -p tcp --dport $3 -m conntrack --ctstate NEW -j ACCEPT" >> "$rules"
}

public_port() {
    # Docker Desktop и rootless Docker могут по-разному транслировать адрес
    # клиента опубликованного порта. Разрешаем хост, но не участников SEPS.
    for subnet in 10 11 12 13; do
        printf '%s\n' "-A INPUT -s $prefix.$subnet.1 -p tcp --dport $1 -j ACCEPT" >> "$rules"
        printf '%s\n' "-A INPUT -s $prefix.$subnet.0/29 -p tcp --dport $1 -j DROP" >> "$rules"
    done
    printf '%s\n' "-A INPUT -p tcp --dport $1 -j ACCEPT" >> "$rules"
}

case "$role" in
    event-api)
        allow_out 10.2 10.3 5432
        allow_out 11.2 11.3 6379
        allow_out 12.2 12.3 5672
        public_port 8000
        ;;
    event-worker)
        allow_out 13.2 13.3 5672
        ;;
    event-worker-standby)
        allow_out 13.4 13.3 5672
        ;;
    postgres) allow_in 10.2 10.3 5432 ;;
    redis) allow_in 11.2 11.3 6379 ;;
    rabbitmq)
        allow_in 12.2 12.3 5672
        allow_in 13.2 13.3 5672
        allow_in 13.4 13.3 5672
        public_port 15672
        ;;
    *) echo "Unknown SEPS_ROLE" >&2; exit 1 ;;
esac

echo COMMIT >> "$rules"
iptables-restore --wait 5 < "$rules"
ip6tables-restore --wait 5 <<'RULES'
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
-A OUTPUT -o lo -j ACCEPT
-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
COMMIT
RULES
echo "firewall_applied role=$role policies=DROP namespace=container"

case "$role" in
    event-api|event-worker|event-worker-standby)
        # У процесса приложения не останется capabilities или root UID.
        exec setpriv --reuid=10001 --regid=10001 --init-groups \
            --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
            --no-new-privs -- "$@"
        ;;
    *)
        # Официальный entrypoint сам переключит UID на postgres/redis/rabbitmq.
        # NET_ADMIN удалён до его запуска, включая bounding set потомков.
        exec setpriv --bounding-set=-net_admin --inh-caps=-all \
            --ambient-caps=-all --no-new-privs \
            -- /usr/local/bin/docker-entrypoint.sh "$@"
        ;;
esac
