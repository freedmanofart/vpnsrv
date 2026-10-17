# OpenClaw для Freedom VPN

Изолированный Telegram-помощник для группы Freedom VPN Admin. Контейнер не подключается к сети VPN-сервисов и не получает Docker socket. Единственный инструмент агента — MCP freedomvpn_health, который читает подготовленный read-only снимок из /opt/openclaw-freedomvpn/health/health.json.

## Установка на сервере

1. Создать .env из шаблона и задать отдельный токен Gateway и токен нового Telegram-бота: cp .env.example .env && chmod 600 .env
2. Создать каталоги state, health и workspace; state должен принадлежать UID/GID 1000.
3. Разместить AGENTS.md в workspace/, а Compose-файл и MCP сервер — рядом с .env.
4. Установить openclaw-freedomvpn-health.service и .timer. Сервис публикует в контейнер только очищенные поля health-отчёта.
5. Настроить OpenClaw Codex OAuth, выбрать openai/gpt-6-luna только если модель доступна в каталоге аккаунта, подключить Telegram через channels add --channel telegram --use-env и разрешить только нужный Telegram chat ID.
6. Запустить docker compose up -d.

.env, state/ и актуальный health/health.json содержат секреты или рабочие данные и не должны добавляться в git.

## Доступ Telegram

Для группы используется groupPolicy: allowlist с единственным разрешённым chat ID, groupAllowFrom: ["*"] и requireMention: true. Личные сообщения отключены. У бота остаётся Telegram Privacy Mode, поэтому он получает сообщения с упоминанием и не читает обычный поток группы.

## Управление

Команды Compose выполняются из каталога /opt/openclaw-freedomvpn. Состояние OAuth хранится в state/; не копировать в git или в VPN-проект. Диагностический MCP-инструмент не принимает аргументы и не может выполнять команды или менять конфигурацию.
