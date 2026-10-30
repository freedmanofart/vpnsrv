#!/usr/bin/env python3
"""Test Hysteria2, TUIC and WireGuard URIs with a temporary sing-box client.

Input is a JSON array of exported profiles on stdin. Only profile names,
connection results, and public egress addresses are printed.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit


SING_BOX = os.environ.get("SING_BOX_BIN", "sing-box")
CHECK_URL = os.environ.get("VPN_CHECK_URL", "https://api.ipify.org")


def first(query, name, default=""):
    return query.get(name, [default])[0]


def outbound(uri):
    parts = urlsplit(uri)
    query = parse_qs(parts.query)
    if parts.scheme == "wireguard":
        return {
            "type": "wireguard",
            "tag": "proxy",
            "address": [first(query, "address")],
            "private_key": unquote(parts.username or ""),
            "mtu": int(first(query, "mtu", "1420")),
            "peers": [{
                "address": parts.hostname,
                "port": parts.port,
                "public_key": first(query, "publickey"),
                "allowed_ips": ["0.0.0.0/0"],
            }],
        }
    if parts.scheme not in {"hysteria2", "tuic"}:
        return None
    tls = {"enabled": True, "server_name": first(query, "sni", parts.hostname)}
    if first(query, "alpn"):
        tls["alpn"] = [value for value in first(query, "alpn").split(",") if value]
    result = {
        "type": parts.scheme,
        "tag": "proxy",
        "server": parts.hostname,
        "server_port": parts.port,
        "tls": tls,
    }
    if parts.scheme == "hysteria2":
        result["password"] = unquote(parts.username or "")
    else:
        result["uuid"] = unquote(parts.username or "")
        result["password"] = unquote(parts.password or "")
        result["congestion_control"] = first(query, "congestion_control", "cubic")
        result["udp_relay_mode"] = first(query, "udp_relay_mode", "native")
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_one(profile):
    proxy = outbound(profile["config"])
    if proxy is None:
        return "SKIP", "unsupported protocol"
    port = free_port()
    config = {
        "log": {"level": "warn"},
        "inbounds": [{"type": "socks", "tag": "local", "listen": "127.0.0.1", "listen_port": port}],
        "route": {"final": "proxy"},
    }
    if proxy["type"] == "wireguard":
        config["endpoints"] = [proxy]
        config["outbounds"] = [{"type": "direct", "tag": "direct"}]
    else:
        config["outbounds"] = [proxy, {"type": "direct", "tag": "direct"}]
    with tempfile.TemporaryDirectory(prefix="vpn-quic-") as directory:
        os.chmod(directory, 0o700)
        path = os.path.join(directory, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(config, handle)
        check = subprocess.run([SING_BOX, "check", "-c", path], capture_output=True, text=True, timeout=10)
        if check.returncode:
            return "BAD_CONFIG", (check.stderr or check.stdout).splitlines()[-1][:180]
        process = subprocess.Popen([SING_BOX, "run", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(30):
                if process.poll() is not None:
                    return "CLIENT_EXIT", "local sing-box exited"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            request = subprocess.run(
                ["curl", "--silent", "--show-error", "--max-time", "14", "--proxy", f"socks5h://127.0.0.1:{port}", CHECK_URL],
                capture_output=True, text=True, timeout=17,
            )
            if request.returncode:
                lines = (process.stderr.readline() if process.poll() is not None else "").strip()
                return "FAIL", (request.stderr or lines or "curl failed").splitlines()[-1][:180]
            return "OK", request.stdout.strip()[:80]
        finally:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def main():
    for profile in json.load(sys.stdin):
        try:
            status, detail = run_one(profile)
        except Exception as exc:
            status, detail = "ERROR", f"{type(exc).__name__}: {str(exc)[:100]}"
        print(f"{profile['name']}\t{status}\t{detail}", flush=True)


if __name__ == "__main__":
    main()
