#!/usr/bin/env python3
"""Write a detailed Tailscale/Funnel health snapshot for the admin API."""

from __future__ import annotations

import json
import ipaddress
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


HOST = os.environ.get("TAILSCALE_HEALTH_HOST", "freedomvpn.taile485ac.ts.net")
HEALTH_FILE = Path(
    os.environ.get(
        "TAILSCALE_HEALTH_FILE",
            "/home/freedman/projects/backup/vpn-service/tailscale-health.json",
    )
)
# Tailscale split DNS resolves this name to the master's private tailnet IP,
# while the public Funnel addresses are returned by a public resolver. Cloudflare
# DNS is periodically blocked from Russian networks, so use Google DNS and a
# short cache of previously verified public answers as a fallback.
DNS_RESOLVERS = (("8.8.8.8", "8.8.8.8"),)
RELAY_IP_CACHE_FILE = HEALTH_FILE.with_name("tailscale-health-relay-ips.json")
RELAY_IP_CACHE_MAX_AGE = 900
PUBLIC_ROUTES = (
    ("/", "200"),
    ("/cabinet", "200"),
    ("/admin", "401"),
    ("/plans", "401"),
    ("/payment-methods", "401"),
)


def run(command: list[str], timeout: float = 8) -> tuple[int, str, str, float]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip(), round(
            (time.monotonic() - started) * 1000, 2
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc), round((time.monotonic() - started) * 1000, 2)


def add(
    checks: list[dict],
    check_id: str,
    name: str,
    status: str,
    details: str,
    latency=None,
    *,
    alert: bool = True,
):
    item = {"id": check_id, "name": name, "status": status, "details": details[:700]}
    if latency is not None:
        item["latency_ms"] = latency
    if not alert:
        item["alert"] = False
    checks.append(item)


def parse_json_output(output: str):
    """Accept CLI log lines that some Tailscale versions print before JSON."""
    start = output.find("{")
    if start < 0:
        raise json.JSONDecodeError("JSON object not found", output, 0)
    payload, _ = json.JSONDecoder().raw_decode(output[start:])
    return payload


def cached_relay_ips() -> tuple[set[str], int | None]:
    try:
        payload = json.loads(RELAY_IP_CACHE_FILE.read_text(encoding="utf-8"))
        generated_at = datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - generated_at).total_seconds()))
        if age_seconds > RELAY_IP_CACHE_MAX_AGE:
            return set(), age_seconds
        addresses = {
            value for value in payload.get("addresses", [])
            if isinstance(value, str)
            and ipaddress.ip_address(value).version == 4
            and ipaddress.ip_address(value).is_global
        }
        return addresses, age_seconds
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return set(), None


def service_check(checks: list[dict], unit: str, name: str, check_id: str | None = None):
    code, output, error, latency = run(["systemctl", "is-active", unit])
    state = output or error or "unknown"
    add(
        checks,
        check_id or f"systemd:{unit}",
        name,
        "online" if code == 0 and state == "active" else "offline",
        f"{unit}: {state}",
        latency,
    )


def http_check(url: str, *, check_id: str, name: str, host_header: str | None = None):
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "FreedomVPN-health/1.0"})
    if host_header:
        request.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            code = response.status
        status = "online" if 200 <= code < 400 else "offline"
        details = f"HTTP {code}"
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        status, details = "offline", f"HTTP probe failed: {exc}"
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "details": details[:700],
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
    }


def main() -> int:
    checks: list[dict] = []
    service_check(checks, "tailscaled.service", "Tailscale daemon")
    service_check(checks, "vpn-tailscale-funnel.service", "Tailscale Funnel service")
    service_check(
        checks,
        "freedomvpn-funnel-watch.timer",
        "Public Funnel watchdog timer",
    )

    code, stdout, stderr, latency = run(["tailscale", "status", "--json"], timeout=12)
    tailnet = None
    if code == 0:
        try:
            tailnet = parse_json_output(stdout)
        except json.JSONDecodeError as exc:
            stderr = f"Invalid Tailscale status JSON: {exc}"
    if tailnet is None:
        reason = stderr or f"tailscale status exit={code}"
        add(checks, "tailscale:backend", "Tailscale backend", "offline", reason, latency)
        add(checks, "tailscale:self", "Tailscale self node", "offline", "status unavailable")
        add(checks, "tailscale:ingress-peers", "Online ingress peers", "offline", "status unavailable")
    else:
        backend = str(tailnet.get("BackendState") or "unknown")
        add(
            checks,
            "tailscale:backend",
            "Tailscale backend",
            "online" if backend == "Running" else "offline",
            f"BackendState={backend}",
            latency,
        )
        self_node = tailnet.get("Self") or {}
        is_online = self_node.get("Online") is True
        add(
            checks,
            "tailscale:self",
            "Tailscale self node",
            "online" if is_online else "offline",
            f"self Online={str(self_node.get('Online', 'unknown')).lower()}",
        )
        peers = tailnet.get("Peer") or tailnet.get("Peers") or {}
        ingress = [
            peer
            for peer in peers.values()
            if "tag:ingress" in (peer.get("Tags") or []) and peer.get("Online") is True
        ]
        add(
            checks,
            "tailscale:ingress-peers",
            "Online ingress peers",
            "online" if ingress else "offline",
            f"online tag:ingress peers={len(ingress)}",
        )

    code, stdout, stderr, latency = run(["tailscale", "funnel", "status", "--json"], timeout=12)
    funnel = None
    if code == 0:
        try:
            funnel = parse_json_output(stdout)
        except json.JSONDecodeError as exc:
            stderr = f"Invalid Funnel JSON: {exc}"
    funnel_ok = False
    if funnel is not None:
        tcp = (funnel.get("TCP") or {}).get("443") or {}
        web = (funnel.get("Web") or {}).get(f"{HOST}:443") or {}
        handlers = web.get("Handlers") or {}
        proxy = (handlers.get("/") or {}).get("Proxy")
        allowed = (funnel.get("AllowFunnel") or {}).get(f"{HOST}:443") is True
        funnel_ok = tcp.get("HTTPS") is True and proxy == "http://127.0.0.1:8000" and allowed
        details = f"443 HTTPS={tcp.get('HTTPS')}; / proxy={proxy}; Funnel allowed={allowed}"
    else:
        details = stderr or f"tailscale funnel status exit={code}"
    add(
        checks,
        "tailscale:funnel-config",
        "Funnel configuration",
        "online" if funnel_ok else "offline",
        details,
        latency,
    )

    code, stdout, stderr, latency = run(["tailscale", "netcheck", "--format=json"], timeout=20)
    netcheck = None
    if code == 0:
        try:
            netcheck = parse_json_output(stdout)
        except json.JSONDecodeError as exc:
            stderr = f"Invalid netcheck JSON: {exc}"
    if netcheck is None:
        add(
            checks,
            "tailscale:network-path",
            "Tailscale network path",
            "degraded",
            stderr or f"netcheck exit={code}",
            latency,
        )
    else:
        udp = netcheck.get("UDP") is True
        derp = int(netcheck.get("PreferredDERP") or 0)
        status = "online" if udp else ("degraded" if derp else "offline")
        add(
            checks,
            "tailscale:network-path",
            "Tailscale network path",
            status,
            f"UDP={str(udp).lower()}, preferred DERP region={derp or 'none'}",
            latency,
            alert=status != "degraded",
        )

    checks.append(
        http_check(
            "http://127.0.0.1:8000/health",
            check_id="tailscale:local-api",
            name="Local API backend",
        )
    )

    addresses: set[str] = set()
    resolver_errors = []
    for resolver_name, resolver_address in DNS_RESOLVERS:
        command = ["dig", "+short", "+time=2", "+tries=1", HOST, "A"]
        if resolver_address:
            command.append(f"@{resolver_address}")
        dig_code, dig_out, dig_err, dig_ms = run(command, timeout=4)
        found = set()
        for line in dig_out.splitlines():
            value = line.strip()
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.version == 4 and address.is_global:
                found.add(value)
        if dig_code == 0 and found:
            addresses.update(found)
            details = f"{resolver_name} resolved {len(found)} public A record(s)"
            status = "online"
        else:
            details = f"{resolver_name} lookup failed: {dig_err or dig_out or 'no public A records'}"
            status = "offline"
            resolver_errors.append(resolver_name)
        add(
            checks,
            f"tailscale:dns:{resolver_name}",
            f"Public DNS {resolver_name} for Funnel",
            status,
            details,
            dig_ms,
            alert=False,
        )

    using_cached_addresses = False
    cache_age_seconds = None
    if addresses:
        RELAY_IP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cache_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "addresses": sorted(addresses),
        }
        cache_temp = RELAY_IP_CACHE_FILE.with_suffix(".tmp")
        cache_temp.write_text(json.dumps(cache_payload) + "\n", encoding="utf-8")
        os.replace(cache_temp, RELAY_IP_CACHE_FILE)
    else:
        addresses, cache_age_seconds = cached_relay_ips()
        using_cached_addresses = bool(addresses)

    if addresses:
        address_health = []
        for address in sorted(addresses):
            started = time.monotonic()
            try:
                # --resolve pins the TLS SNI and certificate hostname while selecting the
                # public Funnel relay address returned by DNS.
                curl_code, curl_out, curl_err, _ = run(
                    [
                        "curl",
                        "--silent",
                        "--show-error",
                        "--output",
                        "/dev/null",
                        "--write-out",
                        "%{http_code}",
                        "--connect-timeout",
                        "6",
                        "--max-time",
                        "12",
                        "--resolve",
                        f"{HOST}:443:{address}",
                        f"https://{HOST}/health",
                    ],
                    timeout=14,
                )
                response_code = curl_out.strip()
                ok = curl_code == 0 and response_code == "200"
                details = f"{address}: HTTP {response_code or curl_err or 'probe failed'}"
                status = "online" if ok else "offline"
            except OSError as exc:
                details, status = f"{address}: TCP/443 failed: {exc}", "offline"
            add(
                checks,
                f"tailscale:public-relay:{address}",
                f"Public Funnel relay {address}",
                status,
                details,
                round((time.monotonic() - started) * 1000, 2),
                alert=False,
            )
            relay_ok = status == "online"
            for path, expected_code in PUBLIC_ROUTES:
                started = time.monotonic()
                try:
                    curl_code, curl_out, curl_err, _ = run(
                        [
                            "curl",
                            "--silent",
                            "--show-error",
                            "--location",
                            "--max-redirs",
                            "5",
                            "--output",
                            "/dev/null",
                            "--write-out",
                            "%{http_code}",
                            "--connect-timeout",
                            "6",
                            "--max-time",
                            "12",
                            "--resolve",
                            f"{HOST}:443:{address}",
                            f"https://{HOST}{path}",
                        ],
                        timeout=14,
                    )
                    response_code = curl_out.strip()
                    ok = curl_code == 0 and response_code == expected_code
                    status = "online" if ok else "offline"
                    details = (
                        f"{address}: HTTP {response_code or curl_err or 'probe failed'}"
                        f" (expected {expected_code})"
                    )
                except OSError as exc:
                    status, details = "offline", f"{address}: HTTPS failed: {exc}"
                route_id = path.strip("/").replace("/", "-") or "root"
                add(
                    checks,
                    f"tailscale:public-route:{address}:{route_id}",
                    f"Public route {path} via relay {address}",
                    status,
                    details,
                    round((time.monotonic() - started) * 1000, 2),
                    alert=False,
                )
                relay_ok = relay_ok and status == "online"
            address_health.append((address, relay_ok))
        healthy_relays = sum(1 for _, healthy in address_health if healthy)
        if healthy_relays == len(address_health) and not using_cached_addresses:
            ingress_status = "online"
        elif healthy_relays:
            ingress_status = "degraded"
        else:
            ingress_status = "offline"
        unhealthy_relays = [address for address, healthy in address_health if not healthy]
        ingress_details = (
            f"All public routes passed through {healthy_relays}/{len(address_health)} relay IPs"
        )
        if unhealthy_relays:
            ingress_details += f"; failed relay IPs: {', '.join(unhealthy_relays)}"
        if using_cached_addresses:
            ingress_details += (
                f"; resolver failed, probing cached answers ({cache_age_seconds}s old)"
            )
        add(
            checks,
            "tailscale:public-ingress",
            "Public Funnel ingress",
            ingress_status,
            ingress_details,
            alert=False,
        )
    else:
        add(
            checks,
            "tailscale:public-relays",
            "Public Funnel relays",
            "offline",
            "System DNS returned no public A records for the Funnel hostname",
            alert=False,
        )
        add(
            checks,
            "tailscale:public-ingress",
            "Public Funnel ingress",
            "offline",
            "Cannot probe public routes because system DNS returned no public A records",
            alert=False,
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "online"
        if all(check["status"] == "online" for check in checks)
        else "degraded",
        "details": f"{len(checks)} checks; public DNS errors: {', '.join(resolver_errors) or 'none'}",
        "checks": checks,
    }
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="tailscale-health-", suffix=".tmp", dir=HEALTH_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, HEALTH_FILE)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # Health failures are represented in the snapshot; the unit succeeded when
    # it collected and wrote that snapshot successfully.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
