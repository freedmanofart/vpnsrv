# Deep link Freedom VPN (`freevpn:`)

Этот документ описывает передачу и обновление подписки Freedom VPN из web-сайта
и Telegram-бота. Формат совместим с импортом подписок: приложение получает
HTTPS-адрес подписки и само загружает актуальную конфигурацию.

## Формат ссылки

Канонический deep link:

```text
freevpn://import/<URL-encoded HTTPS subscription URL>
```

Пример:

```text
freevpn://import/https://freedomvpn.example/v1/client/subscription/<signed-token>
```

`freevpn:` — это URI-схема, а рабочий полный формат содержит `//import/`.
В ссылку нельзя помещать VLESS/VMess URI, UUID, пароль, токен API, Telegram ID
или платёжные данные. Внутри находится только подписанный URL подписки.

## Получение ссылок из API

Запрос с сервисным токеном:

```http
GET /vpn/clients/{client_id}/config
Authorization: Bearer <SERVICE_API_TOKEN>
```

Ответ содержит:

```json
{
  "freevpn_subscription_url": "https://freedomvpn.example/v1/client/subscription/<signed-token>",
  "freevpn_import_url": "freevpn://import/https://freedomvpn.example/v1/client/subscription/<signed-token>",
  "freevpn_telegram_import_url": "https://freedomvpn.example/v1/client/freevpn-import/<signed-token>"
}
```

Подписанный токен содержит ID клиента и срок действия. Endpoint подписки
дополнительно проверяет, что клиент активен, поэтому ссылка перестаёт работать
после отзыва клиента или окончания подписки. После продления создавайте новую
ссылку, чтобы срок токена соответствовал новому сроку доступа.

## Telegram-бот

В `InlineKeyboardButton` передавайте `freevpn_telegram_import_url`:

```python
InlineKeyboardButton(
    text="📲 Импортировать в Freedom VPN",
    url=data["freevpn_telegram_import_url"],
)
```

Telegram открывает HTTPS-переходник `/v1/client/freevpn-import/{token}`. Страница
пытается автоматически открыть приложение и также показывает явную кнопку
`Открыть в Freedom VPN` с адресом `freevpn://...`; рядом остаётся обычная ссылка
подписки для резервного сценария. Прямой custom-scheme URL в
кнопку Telegram помещать не следует: WebView может вернуть
`ERR_UNKNOWN_URL_SCHEME`.

Для Android-кнопки переходник использует `intent://` с package приложения:

```text
intent://import/<subscription-url>#Intent;scheme=freevpn;package=org.freedomvpn.app;action=android.intent.action.VIEW;category=android.intent.category.BROWSABLE;end
```

Указание package помогает Telegram WebView однозначно передать ссылку Freedom VPN.

## Web-сайт и личный кабинет

На странице, открытой на устройстве с приложением, можно использовать
`freevpn_import_url`. Для браузеров, которые блокируют custom scheme, используйте
`freevpn_telegram_import_url` — это тот же HTTPS-переходник с кнопкой запуска.

Рекомендуемый HTML:

```html
<a href="https://freedomvpn.example/v1/client/freevpn-import/<signed-token>">
  Импортировать в Freedom VPN
</a>
```

## Требования к мобильному приложению

Клиент должен зарегистрировать схему `freevpn` на Android и iOS и обработать
маршрут `/import/<subscription-url>`:

1. разобрать URL и извлечь вложенный HTTPS-адрес;
2. загрузить его по HTTPS без добавления авторизации;
3. проверить HTTP-код и формат профиля;
4. заменить или добавить профиль и показать результат пользователю;
5. не сохранять подписанный URL в открытых логах.

Приложение должно поддерживать повторное открытие той же ссылки после продления:
сервер вернёт актуальный профиль и новые значения трафика/срока.

## Обновление подписки через бот или сайт

1. Пользователь оплачивает продление в Telegram-боте или web-кабинете.
2. Backend подтверждает платёж и обновляет срок активной подписки клиента.
3. Backend выдаёт новый `freevpn_telegram_import_url` и/или
   `freevpn_import_url`.
4. Пользователь нажимает ссылку, приложение повторно загружает подписку и
   заменяет локальные данные.

Deep link не подтверждает оплату и не продлевает тариф самостоятельно; он только
передаёт приложению подписанный адрес уже оплаченной подписки.

## Проверка на тестовой среде

```bash
curl -H "Authorization: Bearer $SERVICE_API_TOKEN" \
  "$PUBLIC_BASE_URL/vpn/clients/$CLIENT_ID/config" | jq .freevpn_telegram_import_url

curl -i "$PUBLIC_BASE_URL/v1/client/freevpn-import/$SIGNED_TOKEN"
curl -i "$PUBLIC_BASE_URL/v1/client/subscription/$SIGNED_TOKEN"
```

Проверьте, что:

- bridge отвечает `200` и содержит `freevpn://import/`;
- endpoint подписки отвечает `200` только для активного клиента;
- после отзыва клиента оба endpoint отвечают `404`;
- Telegram использует HTTPS bridge, а сайт может использовать bridge или прямой
  `freevpn://` URL;
- в логах нет полного токена и содержимого VPN-конфигурации.
