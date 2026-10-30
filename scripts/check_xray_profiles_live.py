#!/usr/bin/env python3
"""Test exported Xray-compatible profile URIs from JSON on stdin.

Input: [{"name": "...", "config": "vless://..."}, ...]. The URIs and
generated Xray configs are never written to stdout.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit


XRAY = os.environ.get("XRAY_BIN", "/usr/local/x-ui/bin/xray-linux-amd64")
CHECK_URL = os.environ.get("VPN_CHECK_URL", "https://api.ipify.org")


def field(query, key, default=""):
    return query.get(key, [default])[0]


def decode_vmess(uri):
    value = uri.removeprefix("vmess://").split("#", 1)[0]
    return json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))


def make_outbound(uri):
    parts = urlsplit(uri)
    query = parse_qs(parts.query)
    if parts.scheme == "vmess":
        item = decode_vmess(uri)
        address = item["add"]
        port = int(item["port"])
        network = item.get("net") or "tcp"
        user = {"id": item["id"], "alterId": int(item.get("aid") or 0), "security": "auto"}
        outbound = {"tag": "proxy", "protocol": "vmess", "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]}}
        security = "tls" if item.get("tls") == "tls" else "none"
        stream = {"network": network, "security": security}
        if security == "tls":
            stream["tlsSettings"] = {"serverName": item.get("sni") or item.get("host") or address, "allowInsecure": False}
        if network == "ws":
            stream["wsSettings"] = {"path": item.get("path") or "/", "headers": {"Host": item.get("host") or address}}
        outbound["streamSettings"] = stream
        return outbound

    if parts.scheme not in {"vless", "trojan", "ss"}:
        return None
    address = parts.hostname
    port = parts.port
    network = field(query, "type", "tcp")
    security = field(query, "security", "none")
    if parts.scheme == "vless":
        user = {"id": unquote(parts.username or ""), "encryption": field(query, "encryption", "none")}
        if field(query, "flow"):
            user["flow"] = field(query, "flow")
        outbound = {"tag": "proxy", "protocol": "vless", "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]}}
    elif parts.scheme == "trojan":
        outbound = {"tag": "proxy", "protocol": "trojan", "settings": {"servers": [{"address": address, "port": port, "password": unquote(parts.username or "")} ]}}
    else:
        if parts.password is not None:
            userinfo = f"{unquote(parts.username or '')}:{unquote(parts.password)}"
        else:
            userinfo = unquote(parts.username or "")
            if ":" not in userinfo:
                userinfo = base64.urlsafe_b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode()
        method, password = userinfo.split(":", 1)
        return {"tag": "proxy", "protocol": "shadowsocks", "settings": {"servers": [{"address": address, "port": port, "method": method, "password": password}]}}

    stream = {"network": network, "security": security}
    if security == "reality":
        stream["realitySettings"] = {
            "serverName": field(query, "sni"),
            "fingerprint": field(query, "fp", "firefox"),
            "publicKey": field(query, "pbk"),
            "shortId": field(query, "sid"),
            "spiderX": field(query, "spx", "/"),
        }
    elif security == "tls":
        stream["tlsSettings"] = {"serverName": field(query, "sni", field(query, "host", address)), "allowInsecure": False}
    if network == "xhttp":
        settings = {"path": field(query, "path", "/"), "mode": field(query, "mode", "auto")}
        if field(query, "host"):
            settings["host"] = field(query, "host")
        if field(query, "extra"):
            settings["extra"] = json.loads(field(query, "extra"))
        stream["xhttpSettings"] = settings
    elif network == "ws":
        stream["wsSettings"] = {"path": field(query, "path", "/"), "headers": {"Host": field(query, "host", address)}}
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": field(query, "serviceName", field(query, "path", "").strip("/"))}
    outbound["streamSettings"] = stream
    return outbound


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_one(profile):
    outbound = make_outbound(profile["config"])
    if outbound is None:
        return "SKIP", "unsupported protocol"
    port = free_port()
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "local", "listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"auth": "noauth", "udp": False}}],
        "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}],
        "routing": {"rules": [{"type": "field", "inboundTag": ["local"], "outboundTag": "proxy"}]},
    }
    with tempfile.TemporaryDirectory(prefix="vpn-live-") as directory:
        os.chmod(directory, 0o700)
        path = os.path.join(directory, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(config, handle)
        check = subprocess.run([XRAY, "run", "-test", "-c", path], capture_output=True, text=True, timeout=10)
        if check.returncode:
            return "BAD_CONFIG", (check.stderr or check.stdout).splitlines()[-1][:180]
        process = subprocess.Popen([XRAY, "run", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(30):
                if process.poll() is not None:
                    return "XRAY_EXIT", "local Xray exited"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            result = subprocess.run(
                ["curl", "--silent", "--show-error", "--max-time", "12", "--proxy", f"socks5h://127.0.0.1:{port}", CHECK_URL],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode:
                return "FAIL", (result.stderr or "curl failed").splitlines()[-1][:180]
            return "OK", result.stdout.strip()[:80]
        finally:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def main():
    profiles = json.load(sys.stdin)
    for profile in profiles:
        try:
            status, detail = run_one(profile)
        except Exception as exc:
            status, detail = "ERROR", f"{type(exc).__name__}: {str(exc)[:100]}"
        print(f"{profile['name']}\t{status}\t{detail}", flush=True)


if __name__ == "__main__":
    main()
