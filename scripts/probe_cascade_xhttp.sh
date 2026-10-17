#!/usr/bin/env bash
set -euo pipefail

# Run as root on RU to verify that the saved cascade outbound carries traffic.
dir=/root/cascade-xhttp-20261017
config="$dir/probe.json"
log="$dir/probe.log"
port=18999
jq '
  .outbounds |= map(select(.tag == "cascade-sweden" or .tag == "direct")) |
  del(.api, .metrics) |
  .inbounds = [{tag:"probe",listen:"127.0.0.1",port:18999,protocol:"socks",settings:{udp:false}}] |
  .routing = {rules:[{type:"field",inboundTag:["probe"],outboundTag:"cascade-sweden"}]}' \
  "$dir/xray-template.new.json" > "$config"
chmod 600 "$config"
/usr/local/x-ui/bin/xray-linux-amd64 run -test -format json -config "$config" >/dev/null
/usr/local/x-ui/bin/xray-linux-amd64 run -c "$config" > "$log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' EXIT
sleep 2
curl -fsS --max-time 20 --socks5-hostname "127.0.0.1:$port" \
  https://www.cloudflare.com/cdn-cgi/trace | head -1
echo 'cascade XHTTP probe passed'
