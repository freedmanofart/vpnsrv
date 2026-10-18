import dns from "node:dns/promises";
import net from "node:net";

const HOST = "0.0.0.0";
const PORT = Number(process.env.PORT || 3128);
const OPENAI_HOSTS = new Set(["chatgpt.com", "auth.openai.com"]);
const UPSTREAM_HOST = process.env.CODEX_UPSTREAM_PROXY_HOST || "100.117.14.51";
const UPSTREAM_PORT = Number(process.env.CODEX_UPSTREAM_PROXY_PORT || 18791);
const UPSTREAM_ROUTE = process.env.CODEX_UPSTREAM_ROUTE || "sweden";
const MAX_HEADER_BYTES = 32768;

function isPublicIPv4(address) {
  if (net.isIP(address) !== 4) return false;
  const [a, b, c] = address.split(".").map(Number);
  if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
  if (a === 100 && b >= 64 && b <= 127) return false;
  if (a === 169 && b === 254) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  if (a === 192 && b === 168) return false;
  if (a === 192 && b === 0 && c === 0) return false;
  if (a === 192 && b === 0 && c === 2) return false;
  if (a === 192 && b === 88 && c === 99) return false;
  if (a === 198 && (b === 18 || b === 19)) return false;
  if (a === 198 && b === 51 && c === 100) return false;
  if (a === 203 && b === 0 && c === 113) return false;
  return true;
}

function parseAuthority(authority, defaultPort) {
  const value = authority.trim();
  if (!value || value.includes("@")) throw new Error("invalid authority");
  const match = value.match(/^([^:\[\]]+|\[[0-9a-fA-F:.]+\])(?::([0-9]{1,5}))?$/);
  if (!match) throw new Error("invalid authority");
  const host = match[1].replace(/^\[|\]$/g, "").toLowerCase().replace(/\.$/, "");
  const port = match[2] ? Number(match[2]) : defaultPort;
  if (!host || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error("invalid authority");
  return { host, port };
}

async function resolvePublicIPv4(host) {
  if (net.isIP(host) === 4) {
    if (!isPublicIPv4(host)) throw new Error("non-public destination");
    return host;
  }
  if (net.isIP(host) !== 0 || host.endsWith(".localhost") || host.endsWith(".local")) {
    throw new Error("non-public destination");
  }
  const answers = await dns.lookup(host, { all: true, family: 4, verbatim: true });
  const answer = answers.find(({ address }) => isPublicIPv4(address));
  if (!answer) throw new Error("no public IPv4 answer");
  return answer.address;
}

function reply(client, code, reason) {
  if (client.destroyed) return;
  client.end(`HTTP/1.1 ${code} ${reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
}

function log(route, host, result) {
  process.stdout.write(`${new Date().toISOString()} route=${route} host=${host} result=${result}\n`);
}

function pipeTunnel(client, upstream, initialClientData = Buffer.alloc(0), initialUpstreamData = Buffer.alloc(0)) {
  if (initialClientData.length) upstream.write(initialClientData);
  if (initialUpstreamData.length) client.write(initialUpstreamData);
  client.setTimeout(0);
  upstream.setTimeout(0);
  client.pipe(upstream);
  upstream.pipe(client);
  client.on("error", () => upstream.destroy());
  upstream.on("error", () => client.destroy());
}

async function openUpstream(host, port) {
  if (OPENAI_HOSTS.has(host)) {
    if (port !== 443) throw new Error("OpenAI destination must use TLS");
    return await openViaUpstreamProxy(host, port);
  }
  const address = await resolvePublicIPv4(host);
  return await connect(address, port);
}

function connect(host, port) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port });
    socket.setTimeout(12000, () => socket.destroy(new Error("connect timeout")));
    socket.once("connect", () => {
      socket.setTimeout(0);
      resolve(socket);
    });
    socket.once("error", reject);
  });
}

function openViaUpstreamProxy(host, port) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: UPSTREAM_HOST, port: UPSTREAM_PORT });
    socket.setTimeout(12000, () => socket.destroy(new Error("upstream proxy timeout")));
    let response = Buffer.alloc(0);
    socket.once("connect", () => {
      socket.write(`CONNECT ${host}:${port} HTTP/1.1\r\nHost: ${host}:${port}\r\nProxy-Connection: keep-alive\r\n\r\n`);
    });
    const onData = (chunk) => {
      response = Buffer.concat([response, chunk]);
      if (response.length > MAX_HEADER_BYTES) {
        socket.destroy();
        reject(new Error("upstream proxy response too large"));
        return;
      }
      const end = response.indexOf("\r\n\r\n");
      if (end < 0) return;
      const statusLine = response.subarray(0, end).toString("latin1").split("\r\n", 1)[0];
      if (!/^HTTP\/1\.[01] 200(?: |$)/.test(statusLine)) {
        socket.destroy();
        reject(new Error("upstream proxy refused CONNECT"));
        return;
      }
      socket.removeListener("data", onData);
      socket.setTimeout(0);
      resolve({ socket, initialData: response.subarray(end + 4) });
    };
    socket.on("data", onData);
    socket.once("error", reject);
  });
}

function readHeader(client) {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const onData = (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > MAX_HEADER_BYTES) {
        cleanup();
        reject(new Error("request header too large"));
        return;
      }
      const end = buffer.indexOf("\r\n\r\n");
      if (end >= 0) {
        cleanup();
        client.pause();
        resolve({ header: buffer.subarray(0, end + 4), rest: buffer.subarray(end + 4) });
      }
    };
    const onError = (error) => { cleanup(); reject(error); };
    const cleanup = () => {
      client.removeListener("data", onData);
      client.removeListener("error", onError);
      client.removeListener("end", onEnd);
    };
    const onEnd = () => { cleanup(); reject(new Error("request ended")); };
    client.on("data", onData);
    client.once("error", onError);
    client.once("end", onEnd);
    client.resume();
  });
}

async function handle(client) {
  client.on("error", () => {});
  client.setTimeout(15000, () => client.destroy());
  try {
    const { header, rest } = await readHeader(client);
    const text = header.toString("latin1");
    const lines = text.split("\r\n");
    const request = lines[0].split(/\s+/);
    if (request.length !== 3) { reply(client, 400, "Bad Request"); return; }
    const [method, target, version] = request;

    if (method === "CONNECT") {
      const { host, port } = parseAuthority(target, 443);
      if (port !== 443) { reply(client, 403, "Forbidden"); log("deny", host, "port"); return; }
      let upstream;
      let initialUpstreamData = Buffer.alloc(0);
      if (OPENAI_HOSTS.has(host)) {
        const opened = await openViaUpstreamProxy(host, port);
        upstream = opened.socket;
        initialUpstreamData = opened.initialData;
        log(UPSTREAM_ROUTE, host, "connected");
      } else {
        const address = await resolvePublicIPv4(host);
        upstream = await connect(address, port);
        log("direct", host, "connected");
      }
      client.write("HTTP/1.1 200 Connection Established\r\nProxy-Agent: FreedomVPN-Codex-Egress\r\n\r\n");
      pipeTunnel(client, upstream, rest, initialUpstreamData);
      client.resume();
      return;
    }

    const url = new URL(target);
    if (url.protocol !== "http:" || url.port && url.port !== "80" || method === "CONNECT") {
      reply(client, 400, "Bad Request"); return;
    }
    const { host, port } = parseAuthority(url.host, 80);
    if (OPENAI_HOSTS.has(host)) { reply(client, 403, "HTTPS Required"); log("deny", host, "plaintext"); return; }
    if (port !== 80) { reply(client, 403, "Forbidden"); log("deny", host, "port"); return; }
    const address = await resolvePublicIPv4(host);
    const upstream = await connect(address, port);
    const forwardedHeaders = lines.slice(1).filter((line) => line && !/^proxy-(?:authorization|connection):/i.test(line));
    const path = `${url.pathname || "/"}${url.search}`;
    const requestHeader = [`${method} ${path} ${version}`, ...forwardedHeaders, "", ""].join("\r\n");
    upstream.write(requestHeader);
    if (rest.length) upstream.write(rest);
    log("direct", host, "http");
    client.setTimeout(0);
    upstream.setTimeout(0);
    client.pipe(upstream);
    upstream.pipe(client);
    client.on("error", () => upstream.destroy());
    upstream.on("error", () => client.destroy());
    client.resume();
  } catch (error) {
    if (client.destroyed || client.writableEnded) return;
    const message = String(error?.message || "proxy error");
    const blocked = /non-public|public IPv4|invalid authority/.test(message);
    reply(client, blocked ? 403 : 502, blocked ? "Forbidden" : "Bad Gateway");
    log("error", "unknown", error?.code || error?.name || "proxy-error");
  }
}

const server = net.createServer((client) => { void handle(client); });
server.listen(PORT, HOST, () => process.stdout.write(`listening ${HOST}:${PORT}\n`));
server.on("error", (error) => { process.stderr.write(`server error ${error.code || error.name}\n`); process.exit(1); });
