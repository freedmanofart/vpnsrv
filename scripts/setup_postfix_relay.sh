#!/usr/bin/env bash
set -euo pipefail

mail_hostname="${VPN_MAIL_HOSTNAME:-freedomvpn.taile485ac.ts.net}"

dnf install -y postfix
postconf -e "myhostname = $mail_hostname"
postconf -e "myorigin = $mail_hostname"
postconf -e "mydestination = localhost.localdomain, localhost"
postconf -e "inet_interfaces = 127.0.0.1, 172.17.0.1, 172.18.0.1"
postconf -e "mynetworks = 127.0.0.0/8, 172.17.0.0/16, 172.18.0.0/16"
postconf -e "smtpd_relay_restrictions = permit_mynetworks, reject_unauth_destination"
postfix check
systemctl enable --now postfix
systemctl restart postfix

echo "Postfix is available to local Docker containers at host.docker.internal:25"
