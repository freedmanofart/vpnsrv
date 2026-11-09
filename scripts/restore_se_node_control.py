#!/usr/bin/env python3
"""Восстановление HTTPS master→child через Tailscale с сохранением адресов подписок.

Запускать от root на master. Меняются только share_addr у входящих подключений
выбранной ноды и её административный адрес; пользователи и ключи не переписываются.
"""
import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import time
import urllib.parse
import urllib.request

BRIDGE = r'''
import asyncio,json,sys
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.db.models import VPNNodeConfig
from app.services.threexui import ThreeXUIClient
async def main():
    request=json.load(sys.stdin)
    async with AsyncSessionLocal() as db:
        rows=(await db.execute(select(VPNNodeConfig))).scalars().all()
    address=next(r.config['api_address'] for r in rows if r.protocol=='vless' and str(r.config.get('api_address','')).startswith(('http://','https://')))
    panel=ThreeXUIClient(address,timeout=40)
    kw={'json':request['body']} if 'body' in request else {}
    result=await panel._request(request['method'],request['path'],**kw)
    print(json.dumps(result))
asyncio.run(main())
'''


def process(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60, **kwargs)


def api(method, path, body=None):
    request = {'method': method, 'path': path}
    if body is not None:
        request['body'] = body
    result = process(['docker', 'exec', '-i', 'vpn-api', 'python', '-c', BRIDGE], input=json.dumps(request))
    return json.loads(result.stdout)


def fingerprint(links):
    result = []
    for link in links:
        if link.startswith('vmess://'):
            encoded = link.removeprefix('vmess://').split('#', 1)[0]
            data = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4)))
            data.pop('ps', None)
            result.append(json.dumps(data, sort_keys=True))
        else:
            result.append(link.split('#', 1)[0])
    return hashlib.sha256(json.dumps(sorted(result)).encode()).hexdigest()


def node_payload(node):
    keys = ('name', 'remark', 'scheme', 'address', 'port', 'basePath', 'enable',
            'allowPrivateAddress', 'tlsVerifyMode', 'pinnedCertSha256',
            'inboundSyncMode', 'inboundTags', 'outboundTag')
    return {k: node[k] for k in keys if k in node}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-id', type=int, required=True)
    parser.add_argument('--tailnet-name', required=True)
    parser.add_argument('--public-host', required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0
    assert args.node_id > 0
    assert re.fullmatch(r'[A-Za-z0-9.-]+\.ts\.net', args.tailnet_name)
    ipaddress.ip_address(args.public_host)
    os.umask(0o077)
    args.backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

    # Активная БД берётся из окружения работающего x-ui, а не из старого SQLite.
    pid = process(['systemctl', 'show', 'x-ui', '-p', 'MainPID', '--value']).stdout.strip()
    environment = dict(p.split('=', 1) for p in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in p)
    assert environment.get('XUI_DB_TYPE') in ('postgres', 'postgresql')
    dsn = environment['XUI_DB_DSN']
    parsed = urllib.parse.urlsplit(dsn)
    if parsed.scheme in ('postgres', 'postgresql'):
        database = urllib.parse.unquote(parsed.path.lstrip('/'))
    else:
        database = dict(v.split('=', 1) for v in shlex.split(dsn) if '=' in v)['dbname']
    postgres = json.loads(process(['docker', 'inspect', 'postgres']).stdout)[0]
    container_environment = dict(e.split('=', 1) for e in postgres['Config']['Env'] if '=' in e)
    owner = container_environment.get('POSTGRES_USER', 'postgres')

    def sql(statement):
        return process(['docker', 'exec', 'postgres', 'psql', '-X', '-U', owner, '-d', database,
                        '-At', '-v', 'ON_ERROR_STOP=1', '-c', statement]).stdout.strip()

    schemas = sql("SELECT table_schema FROM information_schema.tables WHERE table_name IN ('inbounds','nodes','settings') GROUP BY table_schema HAVING COUNT(*)=3").splitlines()
    assert len(schemas) == 1 and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', schemas[0])
    table = '"' + schemas[0] + '".inbounds'
    node = next(n for n in api('GET', 'nodes/list') if n['id'] == args.node_id)
    assert node['address'] in (args.public_host, args.tailnet_name)
    assert node.get('hasApiToken'), 'У ноды отсутствует сохранённый токен'
    rows = [r for r in api('GET', 'inbounds/list') if r.get('nodeId') == args.node_id]
    assert rows, 'У ноды нет входящих подключений'
    emails = set()
    for row in rows:
        settings = row.get('settings') or {}
        settings = json.loads(settings) if isinstance(settings, str) else settings
        emails.update(c['email'] for c in settings.get('clients', []) if c.get('email'))
    before_links = {email: fingerprint(api('GET', 'clients/links/' + urllib.parse.quote(email, safe=''))) for email in sorted(emails)}
    public_updates = [r for r in rows if r.get('shareAddrStrategy') in ('node', '', None)]
    original_share = {str(r['id']): {'strategy': r.get('shareAddrStrategy') or '', 'address': r.get('shareAddr') or ''} for r in public_updates}
    snapshot = {'node': node, 'share': original_share, 'profiles': before_links, 'database': database, 'schema': schemas[0]}
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = args.backup_dir / ('se-control-before-' + stamp + '.json')
    backup.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print('Резервная копия настроек управления:', backup, flush=True)

    root = 'https://' + args.tailnet_name + ':' + str(node['port']) + '/' + node['basePath'].strip('/') + '/'
    with urllib.request.urlopen(root, timeout=15, context=ssl.create_default_context()) as response:
        assert response.status == 200, 'Недоступна страница панели через tailnet'
    print('HTTPS с мастера: TLS verified, HTTP 200', flush=True)
    target = node_payload(node)
    target.update(address=args.tailnet_name, scheme='https', allowPrivateAddress=True, tlsVerifyMode='verify')
    metadata_changed = False
    node_attempted = False
    try:
        if public_updates:
            ids = ','.join(str(r['id']) for r in public_updates)
            sql(f"UPDATE {table} SET share_addr_strategy='custom',share_addr='{args.public_host}' WHERE node_id={args.node_id} AND id IN ({ids}) AND COALESCE(share_addr_strategy,'') IN ('','node')")
            metadata_changed = True
        for email, old in before_links.items():
            assert fingerprint(api('GET', 'clients/links/' + urllib.parse.quote(email, safe=''))) == old, 'Изменился экспорт после закрепления публичного адреса'
        print('Публичные адреса закреплены; экспорты', len(emails), 'клиентов совпадают', flush=True)
        node_attempted = True
        api('POST', 'nodes/update/' + str(args.node_id), target)
        probe = api('POST', 'nodes/probe/' + str(args.node_id))
        assert probe.get('status') == 'online' and probe.get('xrayState') == 'running', 'Probe не подтвердил работоспособность ноды'
        # Список нод включает состояние фонового runtime и обновляется после probe асинхронно.
        for attempt in range(15):
            state = next(n for n in api('GET', 'nodes/list') if n['id'] == args.node_id)
            if state['status'] == 'online' and state['xrayState'] == 'running':
                break
            if attempt == 14:
                raise RuntimeError('Состояние runtime master не обновилось после успешного probe')
            time.sleep(1)
        for email, old in before_links.items():
            assert fingerprint(api('GET', 'clients/links/' + urllib.parse.quote(email, safe=''))) == old, 'Изменился экспорт после смены API-адреса'
        print(json.dumps({k: state.get(k) for k in ('id', 'name', 'address', 'status', 'xrayState', 'panelVersion', 'xrayVersion', 'latencyMs')}, ensure_ascii=False), flush=True)
        print('Проверка всех экспортов завершена: публичные подключения сохранены', flush=True)
    except BaseException:
        node_restored = not node_attempted
        if node_attempted:
            try:
                api('POST', 'nodes/update/' + str(args.node_id), node_payload(node))
                node_restored = True
            except Exception:
                print('Не удалось автоматически вернуть запись ноды; копия:', backup, flush=True)
        # При неполном откате сохраняем явный public host: внутренний адрес не должен попасть в подписки.
        if metadata_changed and node_restored:
            for ident, old in original_share.items():
                assert ident.isdigit()
                strategy = old['strategy'].replace("'", "''")
                address = old['address'].replace("'", "''")
                sql(f"UPDATE {table} SET share_addr_strategy='{strategy}',share_addr='{address}' WHERE id={ident} AND node_id={args.node_id} AND share_addr='{args.public_host}'")
        raise


if __name__ == '__main__':
    main()
