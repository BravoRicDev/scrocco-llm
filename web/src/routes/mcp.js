import { Router } from "express";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { requireAuth } from "../middleware/auth.js";
import { discoverTools, makeToolHandler } from "../services/mcp-tools.js";
import { logger } from "../services/logger.js";

const router = Router();

// solo identita' agente (JWT-agent o API token agtok_) puo' usare MCP
function requireAgent(req, res, next) {
  if (req.user && (req.user.agent === true || req.user.api_token === true)) return next();
  return res.status(403).json({
    jsonrpc: "2.0",
    error: { code: -32000, message: "MCP richiede un token agente (JWT-agent o agtok_)" },
    id: null,
  });
}

const toolsByLang = new Map();
function getTools(lang) {
  if (!toolsByLang.has(lang)) {
    const tools = discoverTools(lang);
    logger.info(`MCP [${lang}]: ${tools.length} tool registrati (${tools.filter((t) => t.enriched).length} arricchiti)`);
    toolsByLang.set(lang, tools);
  }
  return toolsByLang.get(lang);
}

function buildServer(lang) {
  const server = new McpServer({ name: "scrocco-web-agent", version: "1.0.0" });
  for (const tool of getTools(lang)) {
    server.registerTool(
      tool.name,
      { description: tool.description, inputSchema: tool.inputSchema },
      makeToolHandler(tool)
    );
  }
  return server;
}

router.post("/api/mcp", requireAuth, requireAgent, async (req, res) => {
  try {
    const server = buildServer((res.locals && res.locals.lang) || "en");
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
    res.on("close", () => { transport.close(); server.close(); });
  } catch (err) {
    logger.error(`MCP: errore richiesta: ${err.message}`);
    if (!res.headersSent) {
      res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "errore interno MCP" }, id: null });
    }
  }
});

router.get("/api/mcp", requireAuth, requireAgent, (_req, res) => {
  res.status(405).json({ jsonrpc: "2.0", error: { code: -32000, message: "usa POST" }, id: null });
});

router.delete("/api/mcp", requireAuth, requireAgent, (_req, res) => {
  res.status(405).json({ jsonrpc: "2.0", error: { code: -32000, message: "metodo non consentito" }, id: null });
});

export default router;
