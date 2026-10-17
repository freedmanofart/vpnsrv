#!/usr/bin/env bash
set -euo pipefail

# Run on the RU node as root. "stage" checks the config without taking public 443;
# "activate" switches the public listener after Xray has moved to loopback.
mode=${1:-stage}
case "$mode" in stage) front_port=10480 ;; activate) front_port=443 ;; *) echo 'usage: ru_kit_443_nginx.sh stage|activate' >&2; exit 2 ;; esac

source /etc/kit/kit.env
source /etc/x-ui/install-result.env
[[ $SUB_PATH =~ ^/[A-Za-z0-9_-]{1,64}/$ ]] || exit 2
[[ $XUI_WEB_BASE_PATH =~ ^/?[A-Za-z0-9_-]{1,64}/?$ ]] || exit 2
[[ $XUI_PANEL_PORT =~ ^[0-9]{1,5}$ && $SUB_INTERNAL =~ ^[0-9]{1,5}$ ]] || exit 2
panel_path="/${XUI_WEB_BASE_PATH#/}"
panel_path="${panel_path%/}/"
cert=/root/cert/kit-nipio/fullchain.pem
key=/root/cert/kit-nipio/privkey.pem
[[ -s $cert && -s $key ]] || exit 2

cp -n /etc/nginx/nginx.conf /etc/nginx/nginx.conf.before-ru-kit-443
sed -i '\|^include /etc/nginx/ru-kit-stream.conf;$|d' /etc/nginx/nginx.conf
if ! grep -q '^include /etc/nginx/kit-stream.conf;' /etc/nginx/nginx.conf; then
  printf '\ninclude /etc/nginx/kit-stream.conf;\n' >> /etc/nginx/nginx.conf
fi
rm -f /etc/nginx/sites-enabled/default
rm -f /etc/nginx/conf.d/ru-kit.conf

cat > /etc/nginx/kit-stream.conf <<EOF
stream {
    map \$ssl_preread_server_name \$ru_upstream {
        admin.zab.qmonitoring.ru 127.0.0.1:443;
        avi-stat.ru 127.0.0.1:23691;
        biz.mayak-expertiza.ru 127.0.0.1:8443;
        default 127.0.0.1:10446;
    }
    server {
        listen 176.108.250.142:$front_port;
        ssl_preread on;
        proxy_pass \$ru_upstream;
        proxy_protocol on;
        proxy_connect_timeout 10s;
        proxy_timeout 1h;
    }
}
EOF

cat > /etc/nginx/conf.d/kit.conf <<EOF
server {
    listen 127.0.0.1:10446 ssl http2 proxy_protocol;
    server_name _;
    ssl_certificate $cert;
    ssl_certificate_key $key;
    ssl_protocols TLSv1.2 TLSv1.3;
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;
    server_tokens off;
    absolute_redirect off;
    access_log off;

    location = /vq9gdbxfk9 {
        proxy_pass http://127.0.0.1:2081;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_protocol_addr;
        proxy_read_timeout 1h;
    }
    location = /uark0be8nb {
        proxy_pass http://127.0.0.1:2087;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_protocol_addr;
        proxy_read_timeout 1h;
    }
    location /lds9tydq/ {
        grpc_pass grpc://127.0.0.1:2083;
        grpc_set_header X-Real-IP \$proxy_protocol_addr;
        grpc_read_timeout 1h;
        grpc_send_timeout 1h;
        client_max_body_size 0;
    }
    location $SUB_PATH {
        proxy_pass http://127.0.0.1:$SUB_INTERNAL;
        proxy_set_header Host \$host;
    }
    location $panel_path {
        proxy_pass https://127.0.0.1:$XUI_PANEL_PORT;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$proxy_protocol_addr;
        proxy_set_header X-Forwarded-For \$proxy_protocol_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
    location / {
        root /var/www/kit;
        index index.html;
    }
}
EOF

nginx -t
systemctl enable nginx >/dev/null
systemctl restart nginx
echo "RU KIT Nginx $mode listening on $front_port/tcp"
