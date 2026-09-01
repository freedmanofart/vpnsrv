# Обслуживающие скрипты

Документ относится к ветке `newnode`, где управление нодами выполняет 3x-ui
master. Старые скрипты развёртывания Xray и собственного node-agent удалены.

## Общие правила

- Запускайте команды из корня репозитория.
- Не включайте `set -x`: параметры могут содержать секреты.
- `.env`, резервные копии и `THREEXUI_API_TOKEN` являются секретами.
- Перед изменениями запускайте `python3 scripts/configctl.py validate`.
- Тесты реального платежного или VPN-потока изменяют данные.

## `configctl.py`

Скрипт атомарно изменяет `.env`, сохраняет комментарии, выставляет права `0600`
и маскирует секреты.

```bash
python3 scripts/configctl.py validate
python3 scripts/configctl.py list
python3 scripts/configctl.py get THREEXUI_API_TOKEN
python3 scripts/configctl.py set THREEXUI_VERIFY_TLS true
python3 scripts/configctl.py apply --services api worker
```

Не генерируйте произвольное значение для `THREEXUI_API_TOKEN`: токен должен
быть создан самой master-панелью 3x-ui. Открытое значение показывается один раз.

## `backup.sh`

Создаёт:

1. `vpn-db-<UTC>.dump` — PostgreSQL custom-format dump;
2. `vpn-config-<UTC>.tar.gz` — `.env` и Compose-конфигурация.

```bash
sudo VPN_PROJECT_DIR="$PWD" VPN_BACKUP_DIR=/mnt/secure/vpn \
  scripts/backup.sh
```

Архив содержит API token 3x-ui и должен храниться как секрет. Конфигурация и БД
самой панели 3x-ui в этот backup не входят — для master и child необходимо
настроить отдельные резервные копии средствами панели или ОС.

## `verify_backup.sh`

Разворачивает dump во временную PostgreSQL-БД, проверяет структуру и удаляет
временную БД. Рабочую БД не изменяет.

```bash
sudo scripts/verify_backup.sh \
  /var/backups/vpn-service/vpn-db-20260831T120000Z.dump
```

## E2E-проверки control plane

Перед запуском необходимы тестовые пользователь, тариф и логическая нода,
привязанная к тестовому inbound 3x-ui.

```bash
python3 scripts/e2e_payment_webhook.py
python3 scripts/e2e_device_profile.py
```

Проверяйте, какие сущности создаёт конкретный скрипт, и не запускайте его против
боевой оплаты без отдельного тестового окружения.

## Проверка 3x-ui

Через VPN Admin:

1. открыть раздел Nodes;
2. выполнить Health;
3. убедиться, что master доступен и inbound существует;
4. выполнить Reconcile;
5. проверить `errors=0`.

На master-сервере:

```bash
systemctl status x-ui
journalctl -u x-ui --since=-10m --no-pager
```

Для child на `159.223.22.59` сначала открыть туннель:

```bash
ssh -N -L 2223:127.0.0.1:60628 root@159.223.22.59
```

Затем проверить Nodes/Probe из master и состояние панели child в браузере.

## Firewall новой 3x-ui ноды

`configure_3xui_node_firewall.sh` включает минимальные правила для SSH и доступа
master к панели child через Tailscale. Перед применением он запускает systemd
timer: если оператор не выполнит `confirm`, правила автоматически откатятся.

```bash
sudo scripts/configure_3xui_node_firewall.sh apply \
  --master-ip 100.102.21.123 --port 60628 --timeout 180
```

Не подтверждайте изменения, пока не проверены новая SSH-сессия и соединение
master → child. Полная процедура, ручной откат и восстановление через консоль
описаны в [`new-node-firewall.md`](new-node-firewall.md).

## Sensitive debug

`capture_sensitive_debug.py` получает разрешение через debug-сессию VPN Admin и
может сохранить секретные значения в audit/Loki. В новой архитектуре он не
читает Reality private key: ключ находится в БД 3x-ui, а не в этом проекте.

После диагностики закройте debug-сессию и при необходимости ротируйте затронутые
пароли и токены.
