# Обновление control plane на сервере

Эта инструкция описывает обновление production-каталога
`/home/freedman/vpn-service` из ветки `origin/main`, применение миграций и
проверку сервисов. Команды ниже выполняются из root-shell: репозиторий и Git
metadata должны принадлежать одному оператору. Не чередуйте `git` от `root` и
`freedman`, иначе `.git/FETCH_HEAD` и новые объекты снова получат несовместимые
права.

## Предварительные условия

- рабочая версия запущена через Docker Compose;
- remote `origin` указывает на production-репозиторий;
- `.env` заполнен, не отслеживается Git и имеет права `0600`;
- оператор находится в каталоге проекта и не использует shell tracing
  (`set -x` раскрывает секреты).

Откройте root-shell и проверьте исходное состояние:

```bash
sudo -i
cd /home/freedman/vpn-service

git remote -v
git branch --show-current
git status --short --branch
docker compose ps
```

Production-ветка должна быть `main`. Если `git status --short` показывает
изменения, сначала перейдите к разделу
[«Грязное рабочее дерево»](#грязное-рабочее-дерево).

## Обычное обновление

### 1. Создать резервную копию

Перед получением кода сохраните БД и конфигурацию:

```bash
VPN_PROJECT_DIR="$PWD" scripts/backup.sh
```

Успешный скрипт выводит пути `database=...` и `config=...`. Убедитесь, что оба
файла существуют, и регулярно копируйте их с сервера во внешнее зашифрованное
хранилище. Не продолжайте обновление, если backup завершился с ошибкой.

### 2. Получить изменения

```bash
git fetch --all --prune
git status --short --branch
git log --oneline --decorate HEAD..origin/main
git pull --ff-only origin main
```

`--ff-only` не создаёт merge-коммит на сервере и останавливает обновление, если
локальная ветка разошлась с `origin/main`. После получения кода рабочее дерево
должно быть чистым:

```bash
git status --short --branch
```

### 3. Проверить конфигурацию

```bash
test -f .env || { echo '.env не найден, обновление остановлено' >&2; exit 1; }
chmod 600 .env
python3 scripts/configctl.py validate
docker compose config --quiet
```

Если в новой `.env.example` появились обязательные параметры, добавьте их в
`.env` через `scripts/configctl.py`; не заменяйте production `.env` шаблоном и
не выводите секреты в журнал терминала.

### 4. Собрать образы и применить миграции

```bash
docker compose build api worker bot node-agent
docker compose up -d postgres redis
docker compose stop api worker bot node-agent
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
```

`alembic current` должен показать ревизию с отметкой `(head)`. Миграции
выполняются при остановленных сервисах, которые могут читать или изменять
прикладные данные. Если миграция завершилась с ошибкой, не запускайте приложение
со старым кодом поверх частично изменённой схемы: сохраните вывод команды и
перейдите к разделу [«Диагностика и откат»](#диагностика-и-откат).

### 5. Пересоздать и проверить сервисы

```bash
docker compose up -d --remove-orphans
docker compose ps
curl -fsS http://127.0.0.1:8000/health
docker compose logs --since=10m api worker bot
```

Если на этом же сервере намеренно включён Compose-профиль `node-agent`, примените
его явно и проверьте журнал агента:

```bash
docker compose --profile node-agent up -d --remove-orphans
docker compose logs --since=10m node-agent
```

Все ожидаемые сервисы должны быть в состоянии `Up`/`healthy`, health endpoint
должен завершиться с кодом 0, а в свежих журналах не должно быть циклических
перезапусков, ошибок подключения к БД или неприменённых миграций.

## Грязное рабочее дерево

Не выполняйте `git pull`, `git reset --hard` или `git clean -fd`, пока не
определено происхождение строк `M` и `??` в `git status`. Сначала сохраните
полную копию файлов вне каталога проекта:

```bash
cp -a .env "/root/vpn-service.env.$(date -u +%Y%m%dT%H%M%SZ).backup"
tar --exclude='./.git' --exclude='./.venv' \
  -czf "/root/vpn-service-files-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" .
```

Затем получите remote без изменения рабочего дерева и проверьте ситуацию:

```bash
git fetch --all --prune
git branch -vv
git status --short --branch
git log --oneline --decorate -5
git clean -nd
```

Если `origin/main` является единственным эталоном, локальные изменения не нужны,
backup проверен, а предварительный вывод `git clean -nd` не содержит уникальных
ключей или конфигурации, синхронизируйте каталог:

```bash
git show origin/main:.gitignore | sed -n '/^\.env$/p'
git check-ignore -v .env
git reset --hard origin/main
git clean -nd
git clean -fd
test -f .env || cp -a "$(ls -1t /root/vpn-service.env.*.backup | head -1)" .env
chmod 600 .env
git status --short --branch
```

Обе проверки перед `reset` должны подтверждать, что `.env` исключён из Git.
Повторный `git clean -nd` после `reset` показывает только оставшиеся untracked
пути: изучите этот вывод и запускайте `git clean -fd`, только если в нём нет
уникальных данных. Команда не удаляет ignored-файлы, однако отдельная копия
`.env` всё равно обязательна. После синхронизации продолжите с шага
[«Проверить конфигурацию»](#3-проверить-конфигурацию).

Если локальные изменения уникальны или их происхождение неизвестно, не
сбрасывайте их: перенесите архив на отдельную машину и сравните содержимое с
`origin/main`.

## Ошибка доступа к `.git/FETCH_HEAD`

Ошибка `Permission denied` означает, что Git metadata создавались другим
пользователем. Проверьте владельца:

```bash
id
stat -c '%U:%G %a %n' . .git .git/FETCH_HEAD 2>/dev/null
find .git -maxdepth 2 ! -user root -printf '%u:%g %m %p\n' | head -50
```

Если принято обслуживать проект только из root-shell, нормализуйте владение и
продолжайте запускать все Git-команды от `root`:

```bash
chown -R root:root /home/freedman/vpn-service
chmod 600 /home/freedman/vpn-service/.env
```

Не используйте попеременно `sudo git ...` и обычный `git ...` от
`freedman`.

## Диагностика и откат

При неуспешном запуске сначала соберите состояние и журналы:

```bash
docker compose ps
docker compose logs --tail=200 api
docker compose logs --tail=200 worker
docker compose logs --tail=200 bot
docker compose logs --tail=200 postgres
```

Не откатывайте миграции вслепую: новый код мог уже изменить данные. Сначала
остановите пишущие сервисы и сохраните аварийный дамп. Для полного отката
используйте проверенные DB/config backup и предыдущий Git-коммит, а процедуру
восстановления сначала репетируйте на изолированном узле.

После устранения причины повторите:

```bash
python3 scripts/configctl.py validate
docker compose run --rm api alembic upgrade head
docker compose up -d --remove-orphans
curl -fsS http://127.0.0.1:8000/health
```
