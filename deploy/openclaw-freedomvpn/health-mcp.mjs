import { createInterface } from "node:readline";
import { readFile } from "node:fs/promises";
import { Resolver } from "node:dns/promises";
import https from "node:https";

const supportedVersions = new Set(["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]);
const host = "freedomvpn.taile485ac.ts.net";
const resolvers = ["1.1.1.1", "8.8.8.8"];
const routes = [
  { path: "/health", expected: 200 },
  { path: "/", expected: 200 },
  { path: "/cabinet", expected: 200 },
  { path: "/admin", expected: 401 },
  { path: "/plans", expected: 401 },
  { path: "/payment-methods", expected: 401 },
];
const tool = {
  name: "freedomvpn_health",
  title: "Freedom VPN live health report",
  description: "Run live public DNS and HTTPS checks from the OpenClaw server, then include the latest host health snapshot and its age. This tests reachability from the server network, not every customer's network. Accepts no arguments.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: true },
};

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}
function redact(text) {
  return String(text)
    .replace(/(token|password|secret|authorization|api[_-]?key)=([^&\s]+)/gi, "$1=[redacted]")
    .replace(/\b\d{6,12}:[A-Za-z0-9_-]{30,}\b/g, "[redacted-token]")
    .replace(/https?:\/\/\S+/gi, "[url]")
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, "[address]")
    .slice(0, 600);
}

async function resolvePublicDns(server) {
  const resolver = new Resolver();
  resolver.setServers([server]);
  let timer;
  try {
    const addresses = await Promise.race([
      resolver.resolve4(host),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          resolver.cancel();
          reject(Object.assign(new Error("DNS timeout"), { code: "ETIMEOUT" }));
        }, 3500);
        timer.unref?.();
      }),
    ]);
    return {
      addresses,
      check: {
        name: `Public DNS ${server === resolvers[0] ? "1" : "2"}`,
        status: addresses.length ? "online" : "offline",
        details: addresses.length ? `${addresses.length} public A record(s)` : "No A records",
      },
    };
  } catch (error) {
    return {
      addresses: [],
      check: {
        name: `Public DNS ${server === resolvers[0] ? "1" : "2"}`,
        status: "offline",
        details: `DNS lookup failed (${redact(error?.code ?? "unknown error")})`,
      },
    };
  } finally {
    clearTimeout(timer);
  }
}

function probeRoute(address, path, expected) {
  const started = Date.now();
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      resolve({ ...result, latency_ms: Date.now() - started });
    };
    const request = https.request({
      hostname: host,
      servername: host,
      port: 443,
      method: "GET",
      path,
      headers: { "user-agent": "FreedomVPN-health/2.0" },
      lookup: (_hostname, options, callback) => {
        if (options?.all) callback(null, [{ address, family: 4 }]);
        else callback(null, address, 4);
      },
    }, (response) => {
      const statusCode = response.statusCode ?? 0;
      response.resume();
      response.on("end", () => finish({
        name: `HTTPS ${path} via relay`,
        status: statusCode === expected ? "online" : "offline",
        details: `HTTP ${statusCode}; expected ${expected}`,
      }));
    });
    request.setTimeout(8000, () => request.destroy(new Error("timeout")));
    request.on("error", (error) => finish({
      name: `HTTPS ${path} via relay`,
      status: "offline",
      details: `HTTPS request failed (${redact(error?.code ?? error?.message ?? "unknown error")})`,
    }));
    request.end();
  });
}

async function liveReport() {
  const checkedAt = new Date().toISOString();
  const dnsResults = await Promise.all(resolvers.map(resolvePublicDns));
  const addresses = [...new Set(dnsResults.flatMap((result) => result.addresses))];
  const checks = dnsResults.map((result) => result.check);
  if (!addresses.length) {
    checks.push({ name: "Public HTTPS routes", status: "offline", details: "No public relay addresses resolved" });
  } else {
    const routeResults = await Promise.all(addresses.flatMap((address) =>
      routes.map(async (route) => ({
        ...(await probeRoute(address, route.path, route.expected)),
        name: `HTTPS ${route.path} via relay ${addresses.indexOf(address) + 1}`,
      })),
    ));
    checks.push(...routeResults);
  }
  return {
    checked_at: checkedAt,
    status: checks.every((check) => check.status === "online") ? "online" : "degraded",
    vantage: "OpenClaw server Docker network; client-specific networks are not tested",
    checks,
  };
}

async function hostSnapshot() {
  try {
    const raw = await readFile("/data/health.json", "utf8");
    if (raw.length > 1_000_000) throw new Error("snapshot is too large");
    const source = JSON.parse(raw);
    const generatedAt = source.generated_at ?? "unknown";
    const generatedMs = Date.parse(generatedAt);
    const checks = Array.isArray(source.checks) ? source.checks.slice(0, 100).map((check) => ({
      id: redact(check?.id ?? ""),
      name: redact(check?.name ?? ""),
      status: redact(check?.status ?? "unknown"),
      details: redact(check?.details ?? ""),
      ...(Number.isFinite(check?.latency_ms) ? { latency_ms: check.latency_ms } : {}),
    })) : [];
    return {
      generated_at: redact(generatedAt),
      age_seconds: Number.isFinite(generatedMs) ? Math.max(0, Math.floor((Date.now() - generatedMs) / 1000)) : null,
      status: redact(source.status ?? "unknown"),
      details: redact(source.details ?? ""),
      checks,
    };
  } catch (error) {
    return { status: "unavailable", details: "Host health snapshot is missing or invalid", checks: [] };
  }
}

async function healthReport() {
  const [live, snapshot] = await Promise.all([liveReport(), hostSnapshot()]);
  return { live, host_snapshot: snapshot };
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", async (line) => {
  let request;
  try {
    request = JSON.parse(line);
    if (request.jsonrpc !== "2.0") throw new Error("invalid JSON-RPC request");
    if (request.method === "notifications/initialized" || request.method === "notifications/cancelled") return;
    if (request.method === "initialize") {
      const requested = request.params?.protocolVersion;
      send({ jsonrpc: "2.0", id: request.id, result: {
        protocolVersion: supportedVersions.has(requested) ? requested : "2025-06-18",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "freedomvpn-health", version: "2.0.0" },
      }});
      return;
    }
    if (request.method === "ping") {
      send({ jsonrpc: "2.0", id: request.id, result: {} });
      return;
    }
    if (request.method === "tools/list") {
      send({ jsonrpc: "2.0", id: request.id, result: { tools: [tool] } });
      return;
    }
    if (request.method === "tools/call") {
      const params = request.params ?? {};
      const args = params.arguments ?? {};
      if (params.name !== tool.name || Object.keys(args).length > 0) {
        send({ jsonrpc: "2.0", id: request.id, result: {
          content: [{ type: "text", text: "Unsupported tool or arguments." }],
          isError: true,
        }});
        return;
      }
      const report = await healthReport();
      send({ jsonrpc: "2.0", id: request.id, result: {
        content: [{ type: "text", text: JSON.stringify(report, null, 2) }],
        isError: false,
      }});
      return;
    }
    send({ jsonrpc: "2.0", id: request.id, error: { code: -32601, message: "Method not found" } });
  } catch {
    if (request && Object.prototype.hasOwnProperty.call(request, "id")) {
      send({ jsonrpc: "2.0", id: request.id, result: {
        content: [{ type: "text", text: "Live health check failed or returned invalid data." }],
        isError: true,
      }});
    }
    process.stderr.write(`freedomvpn-health MCP request failed (${redact(error?.code ?? error?.name ?? "unknown error")})\n`);
  }
});
