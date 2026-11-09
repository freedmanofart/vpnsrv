#!/usr/bin/env python3
"""Устанавливает проверенную проектную версию CLI и kit-sub с откатом при ошибке."""
import argparse
import ast
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60, **kwargs)


def replace(source, target, mode):
    fd, temporary = tempfile.mkstemp(dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def subscription_probe(email=None):
    config = json.loads(Path('/etc/kit-sub/config.json').read_text())
    connection = sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro', uri=True)
    try:
        if email:
            row = connection.execute(
                "SELECT sub_id FROM clients WHERE email=? AND enable=1", (email,)).fetchone()
        else:
            row = connection.execute(
                "SELECT sub_id FROM clients WHERE enable=1 AND sub_id<>'' ORDER BY id LIMIT 1").fetchone()
    finally:
        connection.close()
    assert row and row[0], 'Нужен включённый клиент с подпиской; укажите --probe-client'
    port = int(config['port'])
    public_host = config['host']

    class LocalTLS(http.client.HTTPSConnection):
        def connect(self):
            self.sock = ssl.create_default_context().wrap_socket(
                socket.create_connection(('127.0.0.1', self.port), timeout=8), server_hostname=public_host)

    request = LocalTLS(public_host, port, timeout=12) if config.get('cert') else http.client.HTTPConnection('127.0.0.1', port, timeout=12)
    try:
        request.request('GET', '/' + config['path'].strip('/') + '/' + row[0],
                        headers={'Host': public_host, 'User-Agent': 'v2rayN/7.0'})
        response = request.getresponse()
        body = response.read()
        assert response.status == 200 and len(body) > 100, f'Подписка: HTTP {response.status}, bytes={len(body)}'
        return {'http': response.status, 'bytes': len(body)}
    finally:
        request.close()


def service_preflight():
    """Проверяет запуск с диска до замены работающего процесса подписки."""
    pending = run(['systemctl', 'show', 'kit-sub', '-p', 'NeedDaemonReload', '--value']).stdout.strip()
    if pending == 'yes':
        raise RuntimeError('Unit kit-sub изменён на диске: проверьте его и выполните systemctl daemon-reload')
    unit = run(['systemctl', 'cat', 'kit-sub']).stdout
    for line in unit.splitlines():
        if line.startswith('LoadCredential=') and ':' in line:
            source = line.split(':', 1)[1].strip()
            if source.startswith('/') and not Path(source).is_file():
                raise RuntimeError('Отсутствует источник systemd credential: ' + source)
    config = json.loads(Path('/etc/kit-sub/config.json').read_text())
    if config.get('cert'):
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(config['cert'], config['key'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--probe-client', help='Email включённого клиента для проверки подписки')
    args = parser.parse_args()
    assert os.geteuid() == 0
    os.umask(0o077)
    if args.probe_only:
        print(subscription_probe(args.probe_client))
        return
    bundle = args.bundle
    metadata = json.loads((bundle / 'provenance.json').read_text())
    with (bundle / 'SHA256SUMS.upstream').open() as manifest:
        run(['ssh-keygen', '-Y', 'verify', '-f', str(bundle / 'allowed_signers'),
             '-I', 'releases@3x-ui-kit', '-n', '3x-ui-kit-release',
             '-s', str(bundle / 'SHA256SUMS.upstream.sig')], stdin=manifest)
    text = (bundle / 'SHA256SUMS.upstream').read_text()
    assert '# 3X-UI KIT 1.2.1' in text
    sums = {line.split()[1]: line.split()[0] for line in text.splitlines() if line and not line.startswith('#')}
    assert sums['scripts/kit.sh'] == metadata['upstream_cli_sha256']
    assert sums['scripts/kit-sub.py'] == hashlib.sha256((bundle / 'kit-sub.py').read_bytes()).hexdigest()
    assert metadata['project_cli_sha256'] == hashlib.sha256((bundle / 'kit.sh').read_bytes()).hexdigest()
    run(['bash', '-n', str(bundle / 'kit.sh')])
    ast.parse((bundle / 'kit-sub.py').read_text())
    service_preflight()
    before = subscription_probe(args.probe_client)
    print('До установки: подпись проверена, подписка', before, flush=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path('/var/backups/freedomvpn') / ('kit-integration-' + stamp)
    backup.mkdir(mode=0o700, parents=True)
    cli = Path('/usr/local/bin/kit')
    server = Path('/usr/local/lib/kit-sub/kit_sub.py')
    assert cli.is_file() and server.is_file()
    shutil.copy2(cli, backup / 'kit.sh')
    shutil.copy2(server, backup / 'kit_sub.py')
    shutil.copy2('/etc/kit-sub/config.json', backup / 'kit-sub-config.json')
    try:
        replace(bundle / 'kit.sh', cli, 0o755)
        replace(bundle / 'kit-sub.py', server, 0o644)
        run(['systemctl', 'restart', 'kit-sub'])
        for attempt in range(15):
            try:
                after = subscription_probe(args.probe_client)
                break
            except (OSError, ssl.SSLError, AssertionError):
                if attempt == 14:
                    raise
                time.sleep(0.3)
        listing = run([str(cli), 'user', 'list'])
        assert 'HTTP 403' not in listing.stderr + listing.stdout
        version = run([str(cli), 'version'])
        assert '3X-UI KIT 1.2.1' in version.stdout
        run(['systemctl', 'is-active', '--quiet', 'kit-sub'])
        print('KIT CLI 1.2.1: список пользователей работает; подпись релиза проверена', flush=True)
        print('kit-sub 1.2.1: подписка', after, 'резервная копия:', backup, flush=True)
    except BaseException:
        replace(backup / 'kit.sh', cli, 0o755)
        replace(backup / 'kit_sub.py', server, 0o644)
        subprocess.run(['systemctl', 'restart', 'kit-sub'], capture_output=True, timeout=30)
        raise


if __name__ == '__main__':
    main()
