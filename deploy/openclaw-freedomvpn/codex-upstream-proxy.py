#!/usr/bin/env python3
import ipaddress
import logging
import os
import re
import select
import socket
import socketserver

LISTEN_HOST = os.environ.get("CODEX_PROXY_BIND", "100.117.14.51")
LISTEN_PORT = int(os.environ.get("CODEX_PROXY_PORT", "18791"))
ALLOWED_CLIENTS = set(filter(None, os.environ.get("CODEX_PROXY_CLIENTS", "100.102.21.123").split(",")))
ALLOWED_HOSTS = {"chatgpt.com", "auth.openai.com"}
MAX_HEADER_BYTES = 16384

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def connect_public(host, port):
    errors = []
    for family, socktype, proto, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        try:
            if not ipaddress.ip_address(address[0].split("%", 1)[0]).is_global:
                continue
            upstream = socket.socket(family, socktype, proto)
            upstream.settimeout(15)
            upstream.connect(address)
            upstream.settimeout(None)
            return upstream
        except OSError as exc:
            errors.append(type(exc).__name__)
    raise OSError("no public upstream address (%s)" % ",".join(errors[-3:]))


class Handler(socketserver.BaseRequestHandler):
    def reply(self, status, message):
        if self.request.fileno() < 0:
            return
        body = (message + "\n").encode("ascii")
        try:
            self.request.sendall(("HTTP/1.1 %s\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % (status, len(body))).encode("ascii") + body)
        except OSError:
            pass

    def handle(self):
        client_ip = self.client_address[0]
        self.request.settimeout(10)
        upstream = None
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) <= MAX_HEADER_BYTES:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
            if len(data) > MAX_HEADER_BYTES:
                self.reply("431 Request Header Fields Too Large", "header too large")
                return
            parts = data.split(b"\r\n", 1)[0].decode("ascii", "replace").split()
            if client_ip not in ALLOWED_CLIENTS:
                logging.warning("deny client=%s reason=client", client_ip)
                self.reply("403 Forbidden", "client denied")
                return
            if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/"):
                logging.warning("deny client=%s reason=method", client_ip)
                self.reply("405 Method Not Allowed", "CONNECT only")
                return
            match = re.fullmatch(r"([A-Za-z0-9.-]+):([0-9]{1,5})", parts[1])
            if not match:
                self.reply("400 Bad Request", "invalid destination")
                return
            host = match.group(1).lower().rstrip(".")
            if host not in ALLOWED_HOSTS or match.group(2) != "443":
                logging.warning("deny client=%s host=%s reason=destination", client_ip, host[:120])
                self.reply("403 Forbidden", "destination denied")
                return

            upstream = connect_public(host, 443)
            logging.info("allow client=%s host=%s", client_ip, host)
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\nProxy-Agent: OpenClawSwedenCodexRelay\r\n\r\n")
            self.request.settimeout(None)
            self.request.setblocking(True)
            if data.partition(b"\r\n\r\n")[2]:
                upstream.sendall(data.partition(b"\r\n\r\n")[2])
            peers = (self.request, upstream)
            while True:
                readable, _, _ = select.select(peers, [], [], 300)
                if not readable:
                    continue
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(payload)
        except (OSError, ValueError):
            return
        finally:
            if upstream is not None:
                upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    server = Server((LISTEN_HOST, LISTEN_PORT), Handler)
    logging.info("listening address=%s port=%d allowed_clients=%s", LISTEN_HOST, LISTEN_PORT, ",".join(sorted(ALLOWED_CLIENTS)))
    server.serve_forever(poll_interval=1)
