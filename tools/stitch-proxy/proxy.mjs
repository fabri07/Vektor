import { StitchProxy } from "@google/stitch-sdk";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

function readConfig() {
  const apiKey = process.env.STITCH_API_KEY?.trim();
  const accessToken = process.env.STITCH_ACCESS_TOKEN?.trim();
  const projectId = process.env.GOOGLE_CLOUD_PROJECT?.trim();
  const baseUrl = process.env.STITCH_HOST?.trim() || "https://stitch.googleapis.com/mcp";
  const timeout = Number.parseInt(process.env.STITCH_TIMEOUT_MS ?? "300000", 10);

  const hasApiKey = Boolean(apiKey);
  const hasOAuth = Boolean(accessToken && projectId);
  if (!hasApiKey && !hasOAuth) {
    throw new Error(
      "Missing Stitch credentials. Set STITCH_API_KEY, or set both STITCH_ACCESS_TOKEN and GOOGLE_CLOUD_PROJECT."
    );
  }

  const config = {
    baseUrl,
    timeout: Number.isFinite(timeout) ? timeout : 300000,
  };

  if (hasApiKey) {
    config.apiKey = apiKey;
  } else {
    config.accessToken = accessToken;
    config.projectId = projectId;
  }

  return config;
}

async function main() {
  const config = readConfig();
  const proxy = new StitchProxy(config);
  const transport = new StdioServerTransport();

  process.stderr.write("[stitch-proxy] Starting Stitch MCP proxy over stdio\n");
  await proxy.start(transport);
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`[stitch-proxy] Failed to start: ${message}\n`);
  process.exit(1);
});
