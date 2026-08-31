# Firewall новой 3x-ui ноды и аварийный откат

Документ описывает включение firewalld на новой child-ноде 3x-ui. Основной
сценарий — master обращается к панели через Tailscale, x-ui слушает только
`127.0.0.1`, а Tailscale Serve проксирует порт `60628` внутри tailnet.

## Текущее диагностическое состояние

На тестовой ноде `159.223.22.59` firewalld временно остановлен для диагностики.
Это не устранило timeout master → `100.89.228.2:60628`, поэтому firewalld не
является причиной текущей ошибки. Дополнительно master возвращает `blocked
private/internal address`: встроенная SSRF-защита 3x-ui запрещает адрес из
диапазона Tailscale `100.64.0.0/10`. До включения ноды необходимо отдельно:

1. разрешить TCP/60628 от master к child в ACL Tailscale;
2. разрешить private/Tailscale node address в настройках или коде 3x-ui master;
3. повторить проверку TCP и API.

Не оставлять firewalld выключенным после завершения диагностики.

## Что делает скрипт

[`configure_3xui_node_firewall.sh`](../scripts/configure_3xui_node_firewall.sh):

- запоминает, были ли firewalld активен и включён в автозагрузку;
- сохраняет вывод `firewall-cmd --list-all-zones` в `/var/backups/firewalld`;
- включает firewalld;
- сохраняет доступ по SSH в зоне `public`;
- разрешает `60628/tcp` только от указанного Tailscale IPv4 master;
- запускает transient systemd timer аварийного отката;
- не открывает панель всему интернету;
- не удаляет существующие правила Docker, VPN и мониторинга;
- после `confirm` оставляет правила и отменяет таймер;
- при ручном или автоматическом `rollback` удаляет только правила, добавленные
  данным запуском, и восстанавливает исходное состояние службы firewalld.

## Подготовка

Скопировать скрипт на child и сделать исполняемым:

```bash
scp scripts/configure_3xui_node_firewall.sh \
  root@159.223.22.59:/usr/local/sbin/configure_3xui_node_firewall
ssh root@159.223.22.59 \
  'chmod 0750 /usr/local/sbin/configure_3xui_node_firewall'
```

На master узнать Tailscale IPv4:

```bash
tailscale ip -4
```

На child до применения правил проверить:

```bash
systemctl is-active x-ui
systemctl is-active tailscaled
tailscale serve status
ss -lntp | grep 60628
```

Ожидается `x-ui` на `127.0.0.1:60628` и `tailscaled` на Tailscale IP child.

## Безопасное включение правил

Оставить текущую SSH-сессию открытой. Запустить с окном отката 3 минуты:

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall apply \
  --master-ip 100.102.21.123 \
  --port 60628 \
  --timeout 180
```

Скрипт напечатает token, например:

```text
20260831T210000Z-12345
```

Не подтверждать изменения сразу. Сначала открыть **новую** SSH-сессию:

```bash
ssh root@159.223.22.59
```

Затем с master проверить Tailscale и TCP:

```bash
tailscale ping 100.89.228.2
curl -v --max-time 10 \
  http://100.89.228.2:60628/panel/api/server/status
```

Ответ HTTP `401`, `403` или `404` подтверждает, что сеть работает. После этого
можно разбирать API token и base path. Timeout означает, что подтверждать
firewall пока нельзя: нужно проверить Tailscale ACL и SSRF-защиту master.

Посмотреть состояние таймера и firewall:

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall status \
  20260831T210000Z-12345
```

Если новая SSH-сессия и API-проверка работают, отменить аварийный откат:

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall confirm \
  20260831T210000Z-12345
```

## Аварийный откат

Если соединение потеряно, ничего не подтверждать. По истечении `--timeout`
systemd автоматически запустит rollback. Если firewalld до запуска был
остановлен, он снова будет остановлен; если был активен, скрипт оставит его
активным и удалит только добавленные этим запуском правила.

Для немедленного ручного отката:

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall rollback \
  20260831T210000Z-12345
```

После автоматического отката проверить журнал:

```bash
sudo journalctl \
  -u 'vpn-node-firewall-rollback-20260831T210000Z-12345.service' \
  --no-pager
```

Если и после таймера SSH не восстановился, открыть web/serial console
провайдера и выполнить:

```bash
sudo systemctl stop firewalld
sudo systemctl status sshd --no-pager
sudo ss -lntp | grep ':22'
```

После восстановления доступа не делать массовый сброс `/etc/firewalld`.
Сначала изучить backup из `/var/backups/firewalld` и существующие правила:

```bash
sudo firewall-cmd --check-config
sudo firewall-cmd --list-all-zones
```

## Проверка после подтверждения

```bash
sudo systemctl is-enabled firewalld
sudo systemctl is-active firewalld
sudo firewall-cmd --zone=public --query-service=ssh
sudo firewall-cmd --zone=public --query-port=60628/tcp
sudo firewall-cmd --zone=public --list-rich-rules
sudo ss -lntp | grep 60628
```

Ожидается:

- firewalld — `enabled` и `active`;
- SSH — разрешён;
- общий публичный порт `60628/tcp` — `no`;
- rich rule разрешает `60628/tcp` только от Tailscale-IP master;
- x-ui не слушает `0.0.0.0:60628`;
- Tailscale Serve публикует порт только внутри tailnet.

## Полностью открытый вариант

Если выбран публичный вариант из
[`3x-ui-master.md`](3x-ui-master.md), этот скрипт не следует применять без
изменений: он намеренно не открывает `60628/tcp` всему интернету. Для публичной
схемы обязательны HTTPS, проверка сертификата, API token, закрытый base path и
отдельное правило cloud firewall. Команды ручного публичного открытия и полного
точечного отката приведены в основной инструкции.
