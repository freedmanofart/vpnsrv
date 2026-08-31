# Обновление control plane на сервере

Инструкция предназначена для перехода и последующих обновлений ветки
`newnode`. В этой ветке Compose не запускает Xray и node-agent: их функции
выполняет установленная отдельно 3x-ui master.

## Предварительные условия

- 3x-ui master работает и имеет настроенный VLESS Reality inbound;
- для VPN API создан `node-sync` token;
- `.env` содержит `THREEXUI_API_TOKEN` и `THREEXUI_VERIFY_TLS=true`;
- `.env` не отслеживается Git и имеет права `0600`;
- создана резервная копия PostgreSQL и конфигурации;
- отдельно создан backup БД master-панели 3x-ui.

## Получение версии

```bash
sudo -i
cd /home/freedman/vpn-service

git status --short --branch
VPN_PROJECT_DIR="$PWD" scripts/backup.sh
git fetch --all --prune
git log --oneline --decorate HEAD..origin/newnode
git pull --ff-only origin newnode
```

Если рабочее дерево грязное, не выполняйте reset. Сначала сохраните diff и
определите владельца локальных изменений.

## Обновление `.env`

Удалите устаревшие параметры при удобном плановом обслуживании:

- `XRAY_API_ADDRESS`;
- `XRAY_INBOUND_TAG`;
- `XRAY_MANAGEMENT_MODE`;
- `NODE_AGENT_TOKEN`;
- `NODE_AGENT_NODE_ID`;
- `NODE_AGENT_INTERVAL_SECONDS`;
- `CONTROL_PLANE_URL`.

Добавьте:

```dotenv
THREEXUI_API_TOKEN=<token-master-со-scope-node-sync>
THREEXUI_VERIFY_TLS=true
```

Проверка:

```bash
chmod 600 .env
python3 scripts/configctl.py validate
docker compose config --quiet
```

## Сборка и миграции

```bash
docker compose build api worker bot
docker compose up -d postgres redis
docker compose stop api worker bot
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
```

Миграция `b91c7d23e640` удаляет таблицу старых credentials node-agent. Перед её
применением сохраните backup. Эти credentials после перехода на 3x-ui не нужны.

## Запуск

```bash
docker compose up -d --remove-orphans
docker compose ps
curl -fsS http://127.0.0.1:8000/health
docker compose logs --since=10m api worker bot
```

`--remove-orphans` удалит старые Compose-контейнеры `vpn-xray` и
`vpn-node-agent`, если они были созданы прежней версией проекта. Это не
останавливает системный сервис `x-ui`.

## Проверка после обновления

1. Открыть VPN Admin через SSH-туннель.
2. Привязать логическую ноду к числовому inbound ID master.
3. Выполнить Health — ожидается `online`.
4. Выполнить Reconcile — ожидается `errors=0`.
5. Создать тестового клиента и проверить его появление в 3x-ui.
6. Импортировать VLESS URI и проверить выходной IP.
7. Отозвать клиента и убедиться, что он удалён из inbound.

## Откат

Откат к старому commit после применения миграции требует восстановления таблицы
node-agent credentials либо downgrade Alembic. Предпочтительный путь:

1. остановить `api`, `worker`, `bot`;
2. восстановить PostgreSQL из backup, созданного перед обновлением;
3. вернуть прежний commit и `.env`;
4. пересобрать прежние Compose-сервисы;
5. проверить состояние до открытия пользовательского трафика.

БД 3x-ui обновляется и восстанавливается отдельно от PostgreSQL control plane.

## Настройка child

Процедура регистрации VPS `159.223.22.59`, API token и TLS описана в
[`3x-ui-master.md`](3x-ui-master.md). Для ручного доступа к панели child:

```bash
ssh -N -L 2223:127.0.0.1:60628 root@159.223.22.59
```
