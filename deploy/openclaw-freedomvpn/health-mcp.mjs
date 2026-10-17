import { createInterface } from "node:readline";
import { readFile } from "node:fs/promises";

const supportedVersions = new Set(["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]);
const tool = {
  name: "freedomvpn_health",
  title: "Freedom VPN read-only health report",
  description: "Return the latest sanitized read-only health snapshot for Freedom VPN. Accepts no arguments.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
};

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}
function redact(text) {
  return String(text)
    .replace(/(token|password|secret|authorization|api[_-]?key)=([^&\s]+)/gi, "$1=[redacted]")
    .replace(/\b\d{6,12}:[A-Za-z0-9_-]{30,}\b/g, "[redacted-token]")
    .slice(0, 600);
}
async function healthReport() {
  const raw = await readFile("/data/health.json", "utf8");
  if (raw.length > 1_000_000) throw new Error("snapshot is too large");
  const source = JSON.parse(raw);
  const checks = Array.isArray(source.checks) ? source.checks.slice(0, 100).map((c) => ({
    id: redact(c?.id ?? ""),
    name: redact(c?.name ?? ""),
    status: redact(c?.status ?? "unknown"),
    details: redact(c?.details ?? ""),
    ...(Number.isFinite(c?.latency_ms) ? { latency_ms: c.latency_ms } : {}),
  })) : [];
  return {
    generated_at: redact(source.generated_at ?? "unknown"),
    status: redact(source.status ?? "unknown"),
    details: redact(source.details ?? ""),
    checks,
  };
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
        serverInfo: { name: "freedomvpn-health", version: "1.0.0" },
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
        content: [{ type: "text", text: "Health snapshot unavailable or invalid." }],
        isError: true,
      }});
    }
    process.stderr.write("freedomvpn-health MCP request failed\n");
  }
});
