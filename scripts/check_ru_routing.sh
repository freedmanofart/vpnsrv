#!/usr/bin/env bash
set -u
set -o pipefail

CONFIG=${CONFIG:-/usr/local/x-ui/bin/config.json}
XRAY=${XRAY:-/usr/local/x-ui/bin/xray-linux-amd64}
ASSETS=${ASSETS:-/usr/local/x-ui/bin}

if [[ ! -r "$CONFIG" || ! -r "$ASSETS/geosite_RU.dat" || ! -r "$ASSETS/geosite.dat" ]]; then
  echo "Required Xray config or GeoSite files are missing under $ASSETS" >&2
  exit 2
fi

echo '== Xray config validation =='
if "$XRAY" run -test -format json -config "$CONFIG" 2>&1 | tail -n 12; then
  :
else
  echo 'Xray config validation failed' >&2
  exit 1
fi

echo
echo '== Routing rules and GeoSite membership =='
python3 - "$CONFIG" "$ASSETS/geosite_RU.dat" "$ASSETS/geosite.dat" <<'PY'
import json
import re
import sys

config_path, custom_path, base_path = sys.argv[1:]

def read_varint(buf, pos):
    value = shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
    raise ValueError('truncated protobuf varint')

def fields(buf):
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:
            size, pos = read_varint(buf, pos)
            value, pos = buf[pos:pos + size], pos + size
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise ValueError(f'unsupported protobuf wire type {wire}')
        yield number, wire, value

def geosites(path):
    result = {}
    with open(path, 'rb') as source:
        data = source.read()
    for number, wire, entry in fields(data):
        if number != 1 or wire != 2:
            continue
        name = None
        domains = []
        for field, kind, value in fields(entry):
            if field == 1 and kind == 2:
                name = value.decode('utf-8', 'replace').casefold()
            elif field == 2 and kind == 2:
                dtype = 0
                domain = None
                for item, item_kind, item_value in fields(value):
                    if item == 1 and item_kind == 0:
                        dtype = item_value
                    elif item == 2 and item_kind == 2:
                        domain = item_value.decode('utf-8', 'replace').casefold()
                if domain:
                    domains.append((dtype, domain))
        if name:
            result[name] = domains
    return result

def matches(items, host):
    host = host.casefold().rstrip('.')
    for dtype, value in items:
        if dtype == 0 and value in host:  # Plain
            return True
        if dtype == 1 and re.search(value, host):  # Regex
            return True
        if dtype == 2 and (host == value or host.endswith('.' + value)):  # RootDomain
            return True
        if dtype == 3 and host == value:  # Full
            return True
    return False

with open(config_path, encoding='utf-8') as source:
    config = json.load(source)
rules = config.get('routing', {}).get('rules', [])
inbounds = sorted(i.get('tag') for i in config.get('inbounds', []) if i.get('tag') != 'api')
custom = geosites(custom_path)
base = geosites(base_path)

def category(table, name):
    return table.get(name.casefold(), [])

blocked = category(custom, 'ru-blocked')
available = category(custom, 'ru-available-only-inside')
ru_category = category(base, 'category-ru')
print(f'Inbound tags ({len(inbounds)}): {", ".join(inbounds)}')
print(f'GeoSite categories: ru-blocked={len(blocked)}, ru-available-only-inside={len(available)}, category-ru={len(ru_category)}')

def rule_for(predicate):
    return next((r for r in rules if predicate(r)), None)

blocked_rule = rule_for(lambda r: any('ru-blocked' in x.casefold() for x in r.get('domain', [])))
direct_domain_rule = rule_for(lambda r: any('ru-available-only-inside' in x.casefold() or 'category-ru' in x.casefold() for x in r.get('domain', [])))
direct_ip_rule = rule_for(lambda r: any(x.casefold() == 'geoip:ru' for x in r.get('ip', [])))
fallback = next((r for r in reversed(rules) if r.get('outboundTag') == 'cascade-sweden'), None)
for label, rule in [('ru-blocked', blocked_rule), ('Russian domains direct', direct_domain_rule), ('geoip:ru direct', direct_ip_rule), ('cascade fallback', fallback)]:
    if rule:
        print(f'{label}: outbound={rule.get("outboundTag")} inboundTags={len(rule.get("inboundTag", [])) or "all"}')
    else:
        print(f'{label}: MISSING')

checks = [
    ('blocked rule points to cascade', blocked_rule and blocked_rule.get('outboundTag') == 'cascade-sweden'),
    ('Russian domain rule points direct', direct_domain_rule and direct_domain_rule.get('outboundTag') == 'direct'),
    ('Russian IP rule points direct', direct_ip_rule and direct_ip_rule.get('outboundTag') == 'direct'),
    ('fallback points to cascade', fallback and fallback.get('outboundTag') == 'cascade-sweden'),
]
for label, rule in [('blocked rule', blocked_rule), ('Russian domain rule', direct_domain_rule), ('Russian IP rule', direct_ip_rule), ('fallback rule', fallback)]:
    if rule and rule.get('inboundTag'):
        missing = sorted(set(inbounds) - set(rule['inboundTag']))
        checks.append((f'{label} covers all inbounds', not missing))
        if missing:
            print(f'{label} missing: {", ".join(missing)}')
for label, ok in checks:
    print(f'{"OK" if ok else "FAIL"}: {label}')

print('Domain classification by the configured domain rules:')
for host in ['militarysummary.com', 'google.com', 'youtube.com', 'yandex.ru', 'gosuslugi.ru']:
    if matches(blocked, host):
        route = 'cascade-sweden (ru-blocked)'
    elif matches(available, host) or matches(ru_category, host):
        route = 'direct (Russian domain list)'
    else:
        route = 'cascade-sweden (fallback)'
    print(f'  {host}: {route}')

if not blocked or not available or not ru_category or any(not ok for _, ok in checks):
    sys.exit(1)
PY
