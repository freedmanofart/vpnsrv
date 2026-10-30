# Главная и дочерние ноды 3x-ui

## Текущая схема

- основной сервер запускает 3x-ui как master;
- удалённые 3x-ui подключаются к master как child nodes;
- VPN API обращается только к REST API master;
- VPN API хранит один основной inbound для provisioning клиента;
- дополнительные протоколы того же клиента подключаются к child inbound'ам через
  `kit_inbound_ids` и публикуются в KIT-подписке.

Продукционные страны, каскадные выходы и список профилей меняются вместе с
состоянием master/child и настройками VPN API. Исторические статусы и старые
инструкции не подтверждают текущее состояние серверов. Целевая схема RU-входа и
каскадных профилей описана в [`cascade-profile-routing.md`](cascade-profile-routing.md).

## Токены

На каждом child создаётся отдельный API token. Если конкретная сборка 3x-ui
поддерживает scopes, ему выдаётся минимальный scope `node-sync`; он сохраняется
в записи Nodes на master. Для VPN API на master создаётся другой отдельный token
и записывается в `.env`:

```dotenv
THREEXUI_API_TOKEN=<token-master-node-sync>
THREEXUI_VERIFY_TLS=true
```

Операции `/panel/api/nodes/*` требуют административного токена master. Такой
токен используется только интерактивным скриптом регистрации ноды и не
передаётся контейнерам приложения.

В поставленной в репозиторий справке 3x-ui API token описан как дающий полные
административные права. Поэтому перед production-деплоем проверьте возможности
именно установленной версии. Если UI не предлагает scopes, считайте оба токена
полноадминистративными, не публикуйте API панели и ограничьте источник запросов
сетевыми правилами.

## Адреса панели и входящие подключения

Не смешивайте два разных соединения:

- `scheme/address/port/basePath` — административный API child;
- `host/port` inbound — публичная точка подключения VPN-клиента.

Панель может работать на HTTPS/443, а VLESS inbound — на другом порту. Web base
path панели никогда не включается в VLESS URI. Порт subscription server 3x-ui
также не является API-портом и не является портом VLESS inbound.

Для production используйте HTTPS и `tlsVerifyMode=verify`, `pin` или `mtls`.
Режим `skip` допустим только для краткой диагностики. Закрытый base path,
токены и Reality private key не фиксируются в Git.

## Связь главной и дочерней нод

Master подключается к child только по внешнему HTTPS-адресу. Сертификат должен
быть действителен, его цепочка должна быть доверена master, а DNS-имя —
совпадать с сертификатом. Приватные адреса нод и оверлейные VPN-сети в текущей
схеме не используются. Для рабочей ноды задавайте `allowPrivateAddress=false` и
`tlsVerifyMode=system`.

Проверка выполняется из master:

```bash
curl -vk --max-time 10 https://<child-host>:<panel-port>/<base-path>/panel/api/server/status
```

Коды `401`/`403` подтверждают сетевую доступность. `404` обычно означает
неверный base path, `EOF` — ошибку listener/reverse proxy, timeout — отсутствие
внешнего маршрута или недоступный порт.

## SSL на IP дочерней ноды

Если панель дочерней ноды опубликована по IP, 3x-ui использует короткоживущий
сертификат Let's Encrypt для IP-адреса. Такой сертификат действует примерно 6
дней, поэтому его нужно регулярно перевыпускать и переустанавливать в пути
панели:

```text
/root/cert/ip/fullchain.pem
/root/cert/ip/privkey.pem
```

Ручная проверка на ноде:

```bash
openssl x509 -in /root/cert/ip/fullchain.pem -noout -subject -issuer -dates
systemctl status x-ui --no-pager
```

Автоматический renew выполняет node-скрипт. На каждой ноде задайте её IP и
фактический каталог сертификата в `/etc/sysconfig/vpn-3xui-ip-cert-renew`;
systemd timer проверяет сертификат каждые 2 дня и обновляет его, когда до срока
 действия остаётся не больше 3 дней:

```bash
sudo THREEXUI_IP_CERT_ADDRESS=89.127.212.239 \
  /usr/local/sbin/renew_3xui_ip_cert.sh
```

Скрипт проверяет срок сертификата и обновляет его только при остатке не более
3 дней. Более ранний запуск переустанавливает текущий сертификат и не создаёт
новый ACME-заказ:

1. использует профиль Let’s Encrypt `shortlived` (обязателен для IP SAN, срок
   сертификата около 160 часов), а если сертификата ещё нет — выполняет
   первичный `--issue --standalone`;
2. устанавливает cert/key в настроенный каталог (по умолчанию `/root/cert/ip/`);
3. выполняет заданный `THREEXUI_IP_CERT_RELOAD_CMD`, обычно перезапускает
   `x-ui` и перечитывает Nginx;
4. печатает `notBefore/notAfter` для быстрой проверки.

Для проверки каждые 2 дня установите скрипт и готовые unit/timer на child-ноду:

```bash
install -m 0755 deploy/node/renew_3xui_ip_cert.sh /usr/local/sbin/renew_3xui_ip_cert.sh
cp deploy/systemd/vpn-3xui-ip-cert-renew.service /etc/systemd/system/
cp deploy/systemd/vpn-3xui-ip-cert-renew.timer /etc/systemd/system/
printf '%s\n' 'THREEXUI_IP_CERT_ADDRESS=<node-ip>' 'THREEXUI_IP_CERT_DIR=/root/cert/ip' > /etc/sysconfig/vpn-3xui-ip-cert-renew
chmod 0600 /etc/sysconfig/vpn-3xui-ip-cert-renew
systemctl daemon-reload
systemctl enable --now vpn-3xui-ip-cert-renew.timer
systemctl list-timers vpn-3xui-ip-cert-renew.timer --all
```

Из `/admin` → `Скрипты` это же действие доступно отдельной кнопкой для каждой
активной ноды. Админка не выполняет SSH сама: она возвращает готовую команду,
которую нужно запустить на master SSH-хосте. Команда копирует node-скрипт на
выбранную ноду, включает timer и сразу запускает renew.

Логи:

```bash
journalctl -u vpn-3xui-ip-cert-renew.service -n 100 --no-pager
```

## Основные и дополнительные входящие подключения

После успешного Probe создайте или импортируйте основной inbound и назначьте его
child. Для базового профиля VPN Admin требуются:

- числовой inbound ID;
- публичные host и port;
- Reality SNI, public key, short ID и spiderX;
- VLESS encryption из inbound;
- xHTTP path, mode, host и диапазон padding.

Для дополнительных вариантов создаются отдельные child-assigned inbound'ы. Все
они должны содержать клиента с тем же UUID и email `vpn-<client_id>`. Добавьте их
числовые IDs в `kit_inbound_ids`, затем выполните reconciliation активных
клиентов. `kit_subscription_all_clients=true` включает выдачу KIT-подписки всем
клиентам этой логической ноды; иначе используйте `kit_subscription_client_ids`.
KIT-ссылка считается доступной только когда панель возвращает минимум два
inbound'а для клиента.

Профили должны создаваться на master с назначением child. Не редактируйте только
child config: следующая синхронизация master перезапишет локальное состояние.
Для нескольких TCP-профилей на одном публичном 443 нужен SNI-мультиплексор на
child, уникальные SNI и свободные loopback-порты. Публичный адрес и порт должны
быть одинаково отражены в host groups 3x-ui, подписке и проверках.

Регистрация новой дочерней ноды выполняется скриптом из
[`add-3x-ui-node.md`](add-3x-ui-node.md).

## Локальный API главной ноды

Если master слушает только loopback, API-контейнер использует локальный proxy
`vpn-threexui-proxy.service`. Он связывает адрес Docker bridge с loopback-портом
3x-ui и не публикует панель в интернет. Фактический gateway проверяется так:

```bash
docker inspect vpn-api --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
sudo systemctl status vpn-threexui-proxy.service --no-pager
```

Прокси работает независимо от процесса x-ui и остаётся на Docker bridge во время
перезапуска панели. Это важно, потому что команда `x-ui restart` может выполнять
отдельные stop/start и не перезапускает зависимые systemd units. Если 3x-ui
online, но создание клиента возвращает `All connection attempts failed`,
проверьте listener на Docker bridge и восстановите proxy:

```bash
sudo systemctl start vpn-threexui-proxy.service
sudo systemctl is-active vpn-threexui-proxy.service
ss -lntp | grep 41026
```

После ручной замены unit-файла выполните `systemctl daemon-reload`. Изменение
порта 3x-ui требует синхронно обновить обе стороны `ExecStart` и `api_address`
логической ноды.

## SSH-прокси для доступа оператора к панели

Панель master 3x-ui не должна торчать наружу как публичный HTTP-сервис. Для
ручной проверки клиентов, inbound и child-нод оператор открывает локальный
SSH-туннель:

```bash
ssh -L 2222:127.0.0.1:41026 root@freedomvpn
```

После этого браузер на операторской машине открывает панель через локальный
порт:

```text
http://localhost:2222/<private-3x-ui-base-path>/panel/clients
```

Как это работает:

1. браузер подключается к `localhost:2222` на вашей машине;
2. SSH шифрует соединение до сервера `freedomvpn`;
3. сервер подключается к своему `127.0.0.1:41026`, где слушает 3x-ui;
4. панель видна только внутри SSH-сессии, без публикации порта в интернет.

Зачем нужен такой proxy:

- закрывает 3x-ui от внешнего сканирования;
- оставляет web base path панели приватным;
- не требует отдельного HTTPS/reverse proxy для панели;
- позволяет открывать master и похожие ноды, которые доступны только через SSH.

Проверка туннеля:

```bash
curl -I http://localhost:2222/<private-3x-ui-base-path>/panel/clients
```

Если порт занят, используйте другой локальный порт:

```bash
ssh -L 2223:127.0.0.1:41026 root@freedomvpn
```

и откройте:

```text
http://localhost:2223/<private-3x-ui-base-path>/panel/clients
```

Важно: `localhost:2222` — это адрес на операторской машине, а не на сервере.
Ссылка работает только пока SSH-команда запущена.

После изменения токена или адреса:

```bash
python3 scripts/configctl.py validate
python3 scripts/configctl.py apply --services api worker
curl -fsS http://127.0.0.1:8000/health
```
