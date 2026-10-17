#!/usr/bin/env bash
set -u

TIMEOUT=${TIMEOUT:-12}
DOMAINS=(militarysummary.com google.com wikipedia.org bbc.com youtube.com yandex.ru gosuslugi.ru)

section() { printf '\n== %s ==\n' "$1"; }

section 'Environment'
printf 'time: '; date '+%Y-%m-%d %H:%M:%S %Z'
printf 'os: %s\n' "$(uname -s)"
if [[ -n "${HTTPS_PROXY:-}${https_proxy:-}${ALL_PROXY:-}${all_proxy:-}" ]]; then
  echo 'proxy environment: set (values hidden)'
else
  echo 'proxy environment: not set'
fi

section 'Default route'
if [[ "$(uname -s)" == Darwin ]] && command -v route >/dev/null 2>&1; then
  route -n get 1.1.1.1 2>/dev/null | awk '/interface:|gateway:/{print}'
elif command -v ip >/dev/null 2>&1; then
  ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"||$i=="via") printf "%s %s ",$i,$(i+1); print ""}' | head -n 1
else
  echo 'route utility not found'
fi

section 'DNS'
for domain in "${DOMAINS[@]}"; do
  if command -v dig >/dev/null 2>&1; then
    result=$(dig +time=2 +tries=1 +short "$domain" A 2>/dev/null | paste -sd ',' -)
  elif command -v getent >/dev/null 2>&1; then
    result=$(getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd ',' -)
  else
    result='no dig/getent'
  fi
  [[ -n "$result" ]] || result='FAILED/empty'
  printf '%-24s %s\n' "$domain" "$result"
done

section 'HTTPS through the device network path'
printf '%-24s %-7s %-8s %-8s %-8s %-8s %-8s\n' 'host' 'curl' 'http' 'dns_s' 'tcp_s' 'tls_s' 'total_s'
curl_error=$(mktemp)
trap 'rm -f "$curl_error"' EXIT
for domain in "${DOMAINS[@]}"; do
  result=$(curl -sS -L --connect-timeout 5 --max-time "$TIMEOUT" -o /dev/null \
    -w '%{http_code} %{time_namelookup} %{time_connect} %{time_appconnect} %{time_total}' \
    "https://${domain}/" 2>"$curl_error")
  status=$?
  if [[ $status -eq 0 ]]; then
    read -r http dns tcp tls total <<<"$result"
    printf '%-24s %-7s %-8s %-8s %-8s %-8s %-8s\n' "$domain" 'OK' "$http" "$dns" "$tcp" "$tls" "$total"
  else
    read -r http dns tcp tls total <<<"$result"
    printf '%-24s %-7s %-8s %-8s %-8s %-8s %-8s\n' "$domain" "ERR:$status" "${http:--}" "${dns:--}" "${tcp:--}" "${tls:--}" "${total:--}"
    error=$(head -n 1 "$curl_error" | tr -d '\r')
    [[ -z "$error" ]] || printf '  %s\n' "$error"
  fi
done

section 'Egress country (IP is not printed)'
for family in 4 6; do
  country=$(curl -"$family" -fsS --connect-timeout 4 --max-time 8 https://ipinfo.io/country 2>/dev/null | tr -d '\r\n')
  if [[ -n "$country" ]]; then
    printf 'IPv%s: %s\n' "$family" "$country"
  else
    printf 'IPv%s: unavailable\n' "$family"
  fi
done

printf '\nDiagnostic complete. HTTP 2xx/3xx means the HTTPS request completed; ERR codes are curl exit codes.\n'
