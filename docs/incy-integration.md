# Интеграция с INCY

Backend выдаёт подписку для INCY по адресу:

```text
/v1/client/subscription/<подписанный-токен>
```

Токен содержит ID клиента и срок действия подписки, поэтому ссылка не требует
авторизации в приложении, но перестаёт работать после отзыва или окончания
доступа.

В ответе подписки передаются:

- рабочая ссылка VLESS текущей ноды;
- название профиля с регионом;
- ссылка на поддержку;
- интервал обновления профиля и срок подписки.

API `/vpn/clients/{client_id}/config` дополнительно возвращает:

- `incy_subscription_url` — URL подписки;
- `incy_import_url` — готовый deep link `incy://import/...`.
- `incy_telegram_import_url` — HTTPS-переходник для кнопки в Telegram. Telegram
  не принимает произвольные custom-scheme URL в `InlineKeyboardButton`, поэтому
  переходник показывает кнопку с Android `intent://` на `incy://import/...`.

Deep link используется в web-кабинете, а HTTPS-переходник — в Telegram-боте. Для приложения
добавлены официальные ссылки INCY для iOS, Android, Windows, macOS, Linux,
Android TV и Apple TV.

Если INCY был активирован через мобильный trial, после импорта оплаченной
подписки приложение должно отправить подписанный токен на общий endpoint:

```http
POST /v1/client/import
Authorization: Bearer <device-access-token>
Content-Type: application/json

{"token":"<signed-token>"}
```

Backend проверяет токен, перепривязывает текущее устройство к владельцу
подписки и возвращает `device_id`, `subscription_id` и `expires_at`. После этого
INCY повторяет `GET /v1/client/profile`; успешным импорт считается только после
получения оплаченного профиля с `trial_id: null`.

## Текущее состояние профилей

В репозитории и в текущей схеме provisioning реально настроен один профиль
Швеции: VLESS Reality xHTTP. Он выдаётся пользователю и импортируется через
INCY.

Профили gRPC, VLESS Vision и Hysteria2 нельзя создать только изменением URL:
для них нужны отдельные рабочие inbounds в 3x-ui, публичные параметры Reality
или TLS, а для Hysteria2 — серверная авторизация. После создания этих inbounds
нужно добавить их provisioning в `api/app/services/threexui.py` и
`api/app/services/provisioning.py`, затем включить ссылки в тело INCY-подписки.
До этого они намеренно не показываются как рабочие подключения.
