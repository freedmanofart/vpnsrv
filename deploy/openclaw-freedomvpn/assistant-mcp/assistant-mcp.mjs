import http from "node:http";
import { timingSafeEqual } from "node:crypto";

const apiBase = (process.env.FREEDOMVPN_ASSISTANT_API_URL || "http://vpn-api:8000").replace(/\/$/, "");
const apiToken = process.env.OPENCLAW_ASSISTANT_TOKEN || "";
const mcpToken = process.env.OPENCLAW_ADMIN_MCP_TOKEN || "";
const port = Number(process.env.PORT || 8080);
const protocolVersions = new Set(["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]);

const tools = [
  {
    name: "freedomvpn_capabilities",
    title: "Freedom VPN assistant capabilities",
    description: "List the support and owner-only admin capabilities available to this assistant.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "freedomvpn_lookup_user",
    title: "Look up a Freedom VPN account",
    description: "Owner-only private lookup by the customer's numeric Telegram ID. Returns only account and latest subscription fields.",
    inputSchema: { type: "object", properties: { telegram_id: { type: "integer", minimum: 1 } }, required: ["telegram_id"], additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_lookup_payments",
    title: "Look up payment status",
    description: "Owner-only private lookup by the customer's numeric Telegram ID. Does not expose provider transaction IDs, receipt contents, or raw payment details.",
    inputSchema: { type: "object", properties: { telegram_id: { type: "integer", minimum: 1 } }, required: ["telegram_id"], additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_get_system_configuration",
    title: "Get a safe configuration summary",
    description: "Owner-only summary of enabled features, integration readiness and configured VLESS nodes. It never returns secrets, raw node configs, IPs or admin endpoints.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_list_plans",
    title: "List tariff plans",
    description: "Show plan prices, duration, connection and traffic limits, and active/public flags.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_list_nodes",
    title: "List VPN nodes",
    description: "Show node name, region, current status, health, capacity and active connections. Never returns IPs, API addresses, or credentials.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_list_pending_actions",
    title: "List pending admin changes",
    description: "Show prepared changes and their current execution status. Use this to inspect uncertain or provider-review refund outcomes.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_prepare_payment_status",
    title: "Prepare a payment status change",
    description: "Prepare a failed/cancelled status change for a non-Platega payment. This does not apply the change. Show the preview and wait for the owner to send the exact confirmation text before using freedomvpn_approve_action.",
    inputSchema: { type: "object", properties: { payment_id: { type: "integer", minimum: 1 }, status: { type: "string", enum: ["failed", "cancelled"] } }, required: ["payment_id", "status"], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_prepare_platega_refund",
    title: "Prepare a Platega refund",
    description: "Checks refund availability and shows the provider deduction. Does not request the refund. Use only when the owner asks; then wait for the exact confirmation text.",
    inputSchema: { type: "object", properties: { payment_id: { type: "integer", minimum: 1 } }, required: ["payment_id"], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  },
  {
    name: "freedomvpn_prepare_plan_update",
    title: "Prepare a tariff change",
    description: "Prepare a plan field update. Allowed fields: name, price, duration_days, max_connections, traffic_limit_gb, is_active, is_public. This does not apply the change. Present before/after values and wait for exact owner confirmation.",
    inputSchema: {
      type: "object", properties: {
        plan_id: { type: "integer", minimum: 1 },
        changes: { type: "object", properties: {
          name: { type: "string", minLength: 1, maxLength: 255 },
          price: { type: "number", exclusiveMinimum: 0, maximum: 1000000 },
          duration_days: { type: "integer", minimum: 1, maximum: 3650 },
          max_connections: { type: "integer", minimum: 1, maximum: 100 },
          traffic_limit_gb: { type: "integer", minimum: 0, maximum: 100000 },
          is_active: { type: "boolean" },
          is_public: { type: "boolean" },
        }, minProperties: 1, additionalProperties: false },
      }, required: ["plan_id", "changes"], additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_prepare_node_update",
    title: "Prepare a VPN node change",
    description: "Prepare a node status (active/disabled), capacity, or region update. This does not apply the change. Show before/after values and any active-connection warning, then wait for exact owner confirmation.",
    inputSchema: {
      type: "object", properties: {
        node_id: { type: "integer", minimum: 1 },
        changes: { type: "object", properties: {
          status: { type: "string", enum: ["active", "disabled"] },
          capacity: { type: "integer", minimum: 1, maximum: 100000 },
          region: { type: ["string", "null"], maxLength: 64 },
        }, minProperties: 1, additionalProperties: false },
      }, required: ["node_id", "changes"], additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
  },
  {
    name: "freedomvpn_approve_action",
    title: "Apply one owner-approved change",
    description: "Applies one pending action only when confirm_text exactly matches `ПОДТВЕРЖДАЮ <action_id>` from the latest owner Telegram DM. Never call this from a group, for a guessed confirmation, or before showing the preview.",
    inputSchema: { type: "object", properties: { action_id: { type: "string", minLength: 32, maxLength: 36 }, confirm_text: { type: "string", minLength: 1, maxLength: 100 } }, required: ["action_id", "confirm_text"], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: true },
  },
];

const capabilities = {
  support_group: ["Ответы по базе знаний и общим вопросам поддержки", "Проверка очищенного health-снимка через отдельный MCP-инструмент"],
  owner_private_chat: [
    "Поиск аккаунта и статуса подписки по Telegram ID",
    "Поиск последних платежей по Telegram ID без реквизитов и чеков",
    "Безопасная сводка конфигурации и готовности подсистем без секретов",
    "Просмотр тарифов и состояния узлов без секретных адресов",
    "Подготовка и подтверждение изменений статусов платежей, тарифов и узлов",
    "Проверка возможности возврата Platega, предварительный расчёт и отправка после точного подтверждения",
  ],
  restrictions: [
    "Админские инструменты должны быть недоступны из групповых чатов",
    "Каждое изменение имеет предпросмотр, одноразовое подтверждение и срок действия 10 минут",
    "Возвраты для провайдеров кроме Platega не автоматизированы",
    "Никакого shell, SQL, Docker socket, произвольных URL или чтения конфигурационных секретов",
  ],
};

async function apiCall(path, { method = "GET", body } = {}) {
  if (!apiToken) throw new Error("assistant authorization is not configured");
  const response = await fetch(`${apiBase}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${apiToken}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(25000),
  });
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = {}; }
  if (!response.ok) {
    const safe = typeof data?.detail === "string" ? data.detail.slice(0, 300) : `API returned HTTP ${response.status}`;
    throw new Error(safe);
  }
  return data;
}

async function callTool(name, args) {
  const empty = Object.keys(args).length === 0;
  if (name === "freedomvpn_capabilities" && empty) return capabilities;
  if (name === "freedomvpn_lookup_user") return apiCall(`/assistant/users/${encodeURIComponent(args.telegram_id)}`);
  if (name === "freedomvpn_lookup_payments") return apiCall(`/assistant/payments?telegram_id=${encodeURIComponent(args.telegram_id)}`);
  if (name === "freedomvpn_get_system_configuration" && empty) return apiCall("/assistant/configuration");
  if (name === "freedomvpn_list_plans" && empty) return apiCall("/assistant/plans");
  if (name === "freedomvpn_list_nodes" && empty) return apiCall("/assistant/nodes");
  if (name === "freedomvpn_list_pending_actions" && empty) return apiCall("/assistant/actions/pending");
  const prepare = {
    freedomvpn_prepare_payment_status: (a) => ({ kind: "payment_status", ...a }),
    freedomvpn_prepare_platega_refund: (a) => ({ kind: "platega_refund", ...a }),
    freedomvpn_prepare_plan_update: (a) => ({ kind: "plan_update", ...a }),
    freedomvpn_prepare_node_update: (a) => ({ kind: "node_update", ...a }),
  }[name];
  if (prepare) return apiCall("/assistant/actions/prepare", { method: "POST", body: prepare(args) });
  if (name === "freedomvpn_approve_action") {
    const actionId = String(args.action_id || "");
    if (!/^[0-9a-f]{32}$/i.test(actionId) && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(actionId)) {
      throw new Error("invalid action ID");
    }
    return apiCall(`/assistant/actions/${encodeURIComponent(actionId)}/approve`, { method: "POST", body: { confirm_text: args.confirm_text } });
  }
  throw new Error("Unsupported tool or arguments");
}

function send(res, status, body, contentType = "application/json") {
  res.writeHead(status, { "Content-Type": contentType, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
  res.end(body);
}

function rpc(res, id, result) {
  send(res, 200, JSON.stringify({ jsonrpc: "2.0", id, result }));
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/healthz") {
    return send(res, 200, "ok\n", "text/plain; charset=utf-8");
  }
  if (req.url !== "/mcp") return send(res, 404, "not found\n", "text/plain; charset=utf-8");
  if (req.method === "GET") return send(res, 405, "method not allowed\n", "text/plain; charset=utf-8");
  if (req.method !== "POST") return send(res, 405, "method not allowed\n", "text/plain; charset=utf-8");
  const suppliedToken = String(req.headers.authorization || "").replace(/^Bearer\s+/i, "");
  const expectedBuffer = Buffer.from(mcpToken);
  const suppliedBuffer = Buffer.from(suppliedToken);
  if (!mcpToken || expectedBuffer.length !== suppliedBuffer.length || !timingSafeEqual(expectedBuffer, suppliedBuffer)) {
    return send(res, 401, "unauthorized\n", "text/plain; charset=utf-8");
  }
  if (!String(req.headers["content-type"] || "").toLowerCase().includes("application/json")) {
    return send(res, 415, "application/json required\n", "text/plain; charset=utf-8");
  }

  let raw = "";
  try {
    for await (const chunk of req) {
      raw += chunk;
      if (raw.length > 128_000) return send(res, 413, "request too large\n", "text/plain; charset=utf-8");
    }
    const request = JSON.parse(raw);
    if (!request || request.jsonrpc !== "2.0" || typeof request.method !== "string") {
      return send(res, 400, "invalid JSON-RPC request\n", "text/plain; charset=utf-8");
    }
    if (request.method.startsWith("notifications/")) return send(res, 202, "", "text/plain; charset=utf-8");
    if (!Object.hasOwn(request, "id")) return send(res, 202, "", "text/plain; charset=utf-8");
    if (request.method === "initialize") {
      const requested = request.params?.protocolVersion;
      return rpc(res, request.id, {
        protocolVersion: protocolVersions.has(requested) ? requested : "2025-06-18",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "freedomvpn-admin", version: "1.0.0" },
      });
    }
    if (request.method === "ping") return rpc(res, request.id, {});
    if (request.method === "tools/list") return rpc(res, request.id, { tools });
    if (request.method === "tools/call") {
      const name = request.params?.name;
      const args = request.params?.arguments ?? {};
      if (typeof name !== "string" || !args || typeof args !== "object" || Array.isArray(args)) {
        return rpc(res, request.id, { content: [{ type: "text", text: "Invalid tool call." }], isError: true });
      }
      try {
        const result = await callTool(name, args);
        return rpc(res, request.id, { content: [{ type: "text", text: JSON.stringify(result, null, 2) }], isError: false });
      } catch (error) {
        const message = error instanceof Error ? error.message : "Assistant tool failed";
        return rpc(res, request.id, { content: [{ type: "text", text: message.slice(0, 400) }], isError: true });
      }
    }
    return send(res, 200, JSON.stringify({ jsonrpc: "2.0", id: request.id, error: { code: -32601, message: "Method not found" } }));
  } catch {
    return send(res, 400, "invalid JSON-RPC payload\n", "text/plain; charset=utf-8");
  }
});

server.listen(port, "0.0.0.0", () => process.stdout.write(`Freedom VPN assistant MCP listening on ${port}\n`));
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
