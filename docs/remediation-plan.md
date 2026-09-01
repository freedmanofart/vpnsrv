# План устранения неисправностей и расхождений

## Цель и порядок работ

Сначала восстановить подтверждаемую доставку magic-link писем, затем уменьшить
зону действия общих credentials и привести фактические маршруты к заявленной
модели доступа. Изменения выполнять на production-хосте с резервной копией
`.env`, базы PostgreSQL и конфигурации Postfix/3x-ui.

## P0 — восстановить отправку почты через relay

### Диагностика

1. Определить фактическую точку отказа: API → локальный Postfix, Postfix → relay
   или relay → почтовый ящик.
2. На production-хосте проверить `systemctl status postfix`, listener на порту
   25, Docker gateway, `postconf -n`, `postqueue -p` и журнал Postfix.
3. Выполнить контролируемый `POST /web/register` на принадлежащий оператору
   адрес и сопоставить время запроса с логом API и Postfix по `X-Request-Id`.
4. Не считать HTTP `200` доказательством доставки: он означает только, что SMTP
   вызов API завершился без ошибки.

### Исправление

1. Выбрать внешний SMTP relay и создать отдельные credentials/app password.
2. Подтвердить sender/domain у провайдера; для собственного домена настроить SPF,
   DKIM и DMARC.
3. Рекомендуемый простой вариант — подключить API к relay напрямую через
   `SMTP_HOST`, `SMTP_PORT=587`, `SMTP_STARTTLS=true` и `SMTP_USERNAME/PASSWORD`.
4. Если нужен локальный smarthost, настроить цепочку API → Postfix → relay по
   разделу [«Настройка SMTP и внешнего relay»](web-cabinet.md#настройка-smtp-и-внешнего-relay).
5. После изменения пересоздать API, отправить тестовую ссылку и подтвердить:
   HTTP `200`, `status=sent`/ответ relay `250`, получение письма, корректный TLS,
   отсутствие письма в spam и прохождение SPF/DKIM/DMARC.
6. Только после успешной проверки выключить
   `CABINET_ALLOW_TEMPORARY_REGISTRATION`.

### Критерий готовности

- 10 последовательных magic-link писем на контролируемые адреса не остаются в
  очереди Postfix;
- relay принимает их без SASL/TLS/sender ошибок;
- ссылка открывает кабинет, а секретный token не появляется в proxy-логах.

## P1 — исправить границы аутентификации

1. Закрыть `/docs`, `/redoc` и `/openapi.json` на reverse proxy либо явно
   отключить публичные FastAPI docs в production.
2. Разделить общий `SERVICE_API_TOKEN` минимум на bot token и operator/script
   token; после этого ограничить router permissions каждого principal.
3. Оставить изменение тарифов, нод, ручных статусов платежей и sensitive debug
   только администратору. Service principal должен выполнять только сценарии,
   действительно нужные Telegram-боту.
4. Добавить rate limiting для `/web/register`, `/web/password/login` и
   `/v1/client/activate`.
5. Добавить серверный logout/revoke web-сессии. Рассмотреть одноразовый magic
   link и отдельную короткую ссылку, обмениваемую на более длительную session
   cookie, вместо одного повторно используемого токена на 365 дней.
6. Проверить реальные scopes установленной 3x-ui. Если токены
   полноадминистративные, ограничить доступ к `/panel/api/*` сетью и отдельными
   credentials для API, master и child.

## P2 — тесты и наблюдаемость

1. Добавить тесты, фиксирующие auth-матрицу всех маршрутов: anonymous, admin,
   service, cabinet user и device.
2. Проверять в CI, что OpenAPI не публикует или proxy не пропускает запрещённые
   production-маршруты.
3. Добавить SMTP smoke test с тестовым relay и тест ошибок TLS/SASL.
4. Запускать полный suite на Python 3.13: закреплённые зависимости проекта не
   устанавливаются в текущем системном Python 3.14.
5. Добавить операционный alert на рост очереди Postfix и повторяющиеся SMTP 4xx,
   не записывая адреса получателей и credentials в метрики.

## Рекомендуемая последовательность релизов

1. Отдельное операционное изменение: relay, sender verification и end-to-end
   проверка писем.
2. Низкорисковый релиз: закрытие OpenAPI и синхронизация документации.
3. Кодовый релиз: раздельные service credentials и route permissions.
4. Кодовый релиз: web-session exchange/revoke и rate limiting.
5. Финальный security regression suite и ротация старых общих токенов.
