#!/usr/bin/env python3
"""Добавляет HTTPS-адрес панели child в tailnet, сохраняя публичную конфигурацию KIT."""
import argparse
import datetime
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def atomic_write(path, text, mode):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tailnet-name', required=True)
    parser.add_argument('--public-host', required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0, 'Нужен root'
    assert re.fullmatch(r'[A-Za-z0-9.-]+\.ts\.net', args.tailnet_name)
    ipaddress.ip_address(args.public_host)
    os.umask(0o077)
    connection = sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro', uri=True)
    try:
        settings = dict(connection.execute('SELECT key,value FROM settings'))
    finally:
        connection.close()
    port = int(settings['webPort'])
    base_path = '/' + settings['webBasePath'].strip('/') + '/'
    assert re.fullmatch(r'/[A-Za-z0-9_/-]+/', base_path), 'Неожиданный base path'
    assert settings.get('webCertFile'), 'Ожидается локальный HTTPS-backend KIT'
    certificate = Path('/root/cert/tailscale/fullchain.pem')
    key = Path('/root/cert/tailscale/privkey.pem')
    pair = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    pair.load_cert_chain(certificate, key)
    run(['openssl', 'x509', '-in', str(certificate), '-noout', '-checkend', '86400'])
    renewal = Path(__file__).with_name('renew_tailnet_panel_cert.sh').read_text()
    domain = args.tailnet_name
    config = f'''# Управление child по HTTPS внутри Tailscale. Сгенерировано configure_child_tailnet_panel.py.
server {{
    listen 127.0.0.1:10446 ssl http2 proxy_protocol;
    server_name {domain};
    ssl_certificate {certificate};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    access_log off;
    absolute_redirect off;
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;
    location ^~ {base_path} {{
        proxy_pass https://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host {args.public_host};
        proxy_set_header X-Real-IP $proxy_protocol_addr;
        proxy_set_header X-Forwarded-For $proxy_protocol_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        # Backend остаётся в loopback того же узла, как в действующем KIT.
        # Сертификат, проверяемый master, обслуживается этим Nginx-vhost.
        proxy_ssl_verify off;
        proxy_redirect https://{args.public_host}/ https://{domain}/;
        proxy_connect_timeout 10s;
        proxy_read_timeout 120s;
    }}
    location / {{ return 404; }}
}}
'''
    service = f'''[Unit]
Description=Renew child panel TLS certificate in Tailscale
After=network-online.target tailscaled.service nginx.service
Wants=network-online.target

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/local/sbin/renew_tailnet_panel_cert.sh {domain}
PrivateTmp=true
NoNewPrivileges=true
'''
    timer = '''[Unit]
Description=Check child panel Tailscale certificate daily

[Timer]
OnCalendar=daily
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
'''
    files = {
        Path('/etc/nginx/conf.d/zz-freedomvpn-tailnet-panel.conf'): (config, 0o600),
        Path('/usr/local/sbin/renew_tailnet_panel_cert.sh'): (renewal, 0o755),
        Path('/etc/systemd/system/vpn-tailnet-panel-cert-renew.service'): (service, 0o644),
        Path('/etc/systemd/system/vpn-tailnet-panel-cert-renew.timer'): (timer, 0o644),
    }
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path('/var/backups/freedomvpn') / ('tailnet-panel-' + stamp)
    backup.mkdir(parents=True, mode=0o700)
    before = {path: path.read_bytes() if path.exists() else None for path in files}
    for i, (path, content) in enumerate(before.items()):
        if content is not None:
            (backup / (str(i) + '-' + path.name)).write_bytes(content)
    (backup / 'before.json').write_text(json.dumps({str(p): v is not None for p, v in before.items()}))
    enabled = subprocess.run(['systemctl', 'is-enabled', '--quiet', 'vpn-tailnet-panel-cert-renew.timer'], capture_output=True).returncode == 0
    try:
        for path, (text, mode) in files.items():
            atomic_write(path, text, mode)
        run(['nginx', '-t'])
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'reload', 'nginx'])

        class LocalConnection(http.client.HTTPSConnection):
            def connect(self):
                self.sock = ssl.create_default_context().wrap_socket(
                    socket.create_connection(('127.0.0.1', 443), timeout=8), server_hostname=domain)

        for attempt in range(20):
            request = LocalConnection(domain, timeout=8)
            try:
                # Новая панель скрывает API без токена ответом 404; проверяем страницу входа.
                request.request('GET', base_path)
                response = request.getresponse()
                response.read()
                assert response.status in (200, 301, 302), f'Панель через vhost: HTTP {response.status}'
                break
            except (OSError, ssl.SSLError, AssertionError):
                if attempt == 19:
                    raise
                time.sleep(0.2)
            finally:
                request.close()
        run(['systemctl', 'enable', '--now', 'vpn-tailnet-panel-cert-renew.timer'])
        print('Tailnet HTTPS:', domain, 'TLS verified, HTTP', response.status)
        print('Renewal timer: enabled; rollback backup:', backup)
    except BaseException:
        if not enabled:
            subprocess.run(['systemctl', 'disable', '--now', 'vpn-tailnet-panel-cert-renew.timer'], capture_output=True)
        for path, content in before.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        subprocess.run(['systemctl', 'daemon-reload'], capture_output=True)
        subprocess.run(['nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
        raise


if __name__ == '__main__':
    main()
