# Firewall новой 3x-ui ноды

Этот документ описывает отдельный скрипт
`scripts/configure_3xui_node_firewall.sh`. Регистрация ноды firewall не меняет.

## Границы безопасности

- не закрывайте текущую SSH-сессию до подтверждения;
- заранее откройте web/serial console провайдера;
- укажите фактический IP master и API-порт child;
- не открывайте административную панель всему интернету без необходимости;
- VPN inbound добавляется отдельным правилом 3x-ui/провайдера и не является
  API-портом панели.

Скрипт сохраняет состояние firewalld, добавляет точечные правила и запускает
transient systemd timer. Без `confirm` изменения автоматически откатываются.

## Установка без применения

```bash
scp scripts/configure_3xui_node_firewall.sh \
  root@<child-host>:/usr/local/sbin/configure_3xui_node_firewall
ssh root@<child-host> \
  'chmod 0750 /usr/local/sbin/configure_3xui_node_firewall'
```

Это только копирует файл и не меняет firewall.

## Предварительная проверка

```bash
systemctl is-active sshd
systemctl is-active x-ui
systemctl is-active firewalld
firewall-cmd --check-config
firewall-cmd --list-all-zones
ss -lntp
```

Запишите SSH-порт, API-порт панели и публичные порты VPN inbound. Сделайте
снимок текущих правил:

```bash
install -d -m 700 /var/backups/firewalld
firewall-cmd --list-all-zones \
  > /var/backups/firewalld/before-$(date -u +%Y%m%dT%H%M%SZ).txt
```

## Применение с аварийным откатом

Оставьте первую SSH-сессию открытой:

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall apply \
  --master-ip <master-ip> \
  --port <child-api-port> \
  --timeout 180
```

Сохраните напечатанный token. До подтверждения:

1. откройте вторую SSH-сессию;
2. с master проверьте TCP/HTTPS панели;
3. выполните Probe в 3x-ui master;
4. проверьте публичное подключение к VPN inbound;
5. посмотрите добавленные rich rules.

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall status <token>
sudo firewall-cmd --zone=public --list-rich-rules
```

Только после успешных проверок:

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall confirm <token>
```

## Откат

Если доступ нарушен, не выполняйте `confirm`: timer восстановит сохранённое
состояние. Немедленный ручной откат:

```bash
sudo /usr/local/sbin/configure_3xui_node_firewall rollback <token>
```

Проверка systemd-журнала:

```bash
sudo journalctl -u "vpn-node-firewall-rollback-<token>.service" --no-pager
```

Если SSH уже недоступен, используйте console провайдера:

```bash
sudo systemctl stop firewalld
sudo systemctl status sshd --no-pager
sudo ss -lntp | grep ':22'
```

После восстановления не удаляйте `/etc/firewalld` и не выполняйте массовый
reset. Сначала сравните текущие правила с backup.

## Возврат к стандартному открытому firewall

Если нужно полностью отменить ограничения скрипта, используйте `rollback` с
token того запуска. Он возвращает исходное состояние, включая состояние службы
firewalld. Если token утрачен, восстановите правила вручную из файла
`/var/backups/firewalld/*` через console провайдера.

Минимальная проверка после отката:

```bash
firewall-cmd --check-config
firewall-cmd --get-active-zones
firewall-cmd --list-all-zones
systemctl is-active sshd x-ui
```

Для публичной панели обязательны HTTPS, API token, закрытый base path и
ограничение источника в cloud firewall/firewalld. Для Tailscale разрешите
master → child API port в tailnet policy и не публикуйте панель наружу.
