import express from "express";
import cors from "cors";
import { Sandbox } from "@e2b/code-interpreter";

const app = express();

app.use(express.json({ limit: "10mb" }));

// You can tighten this later (recommended).
app.use(
  cors({
    origin: "*",
    methods: ["GET", "POST", "OPTIONS"],
    allowedHeaders: ["Content-Type"],
  }),
);

function requireEnv(name) {
  const v = process.env[name];
  if (!v) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return v;
}

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});
app.post("/sandboxes", async (req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const { timeoutMs, metadata, envVars, template } = req.body || {};

    const sandbox = await Sandbox.create({
      apiKey,
      timeoutMs: typeof timeoutMs === "number" ? timeoutMs : 5 * 60 * 1000,
      metadata: metadata || {},
      envs: envVars || {},
      // template is optional in SDK; if you use custom templates, handle it here
      // template: template || undefined,
    });

    res.json({ sandboxId: sandbox.sandboxId });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "Create sandbox failed" });
  }
});
app.post("/sandboxes/:id/run", async (req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const sandboxId = req.params.id;

    const { cmd, cwd, background, envs, timeoutMs } = req.body || {};
    if (!cmd) return res.status(400).json({ error: "Missing cmd" });

    const sandbox = await Sandbox.connect(sandboxId, { apiKey });

    if (background) {
      const proc = await sandbox.commands.run(cmd, {
        cwd: cwd || "/",
        envs: envs || {},
        background: true,
      });
      return res.json({ pid: proc.pid, background: true });
    }

    const result = await sandbox.commands.run(cmd, {
      cwd: cwd || "/",
      envs: envs || {},
      timeoutMs: typeof timeoutMs === "number" ? timeoutMs : 60_000,
    });
     res.json({
      exitCode: result.exitCode,
      stdout: result.stdout,
      stderr: result.stderr,
    });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "Run failed" });
  }
});

// POST /sandboxes/:id/files
// body: { files: [{ path, content }] }
app.post("/sandboxes/:id/files", async (req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const sandboxId = req.params.id;

    const files = Array.isArray(req.body?.files) ? req.body.files : null;
    if (!files?.length) return res.status(400).json({ error: "Missing files" });
    const sandbox = await Sandbox.connect(sandboxId, { apiKey });

    for (const f of files) {
      const path = String(f?.path || "").trim();
      const content = String(f?.content ?? "");
      if (!path) continue;
      await sandbox.files.write(path, content);
    }

    res.json({ ok: true, count: files.length });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "Write files failed" });
  }
});

// GET /sandboxes/:id/host?port=3000
app.get("/sandboxes/:id/host", async (req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const sandboxId = req.params.id;
    const port = Number(req.query.port || 3000);

    const sandbox = await Sandbox.connect(sandboxId, { apiKey });
    const host = sandbox.getHost(port);
    const url = `https://${host}`;
     res.json({ host, url, port });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "Host failed" });
  }
});

const port = Number(process.env.PORT || 8080);
app.listen(port, () => {
  console.log(`e2b-proxy listening on :${port}`);})