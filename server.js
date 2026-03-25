import express from "express";
import cors from "cors";
import { Sandbox } from "@e2b/code-interpreter";

const app = express();

app.use(express.json({ limit: "10mb" }));

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

// ─── Health Check ───
app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

// ─── Create Sandbox ───
app.post("/sandboxes", async (req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const { timeoutMs, metadata, envVars, template } = req.body || {};

    // Use template from request body, or fall back to E2B_TEMPLATE env var
    const tpl = template || process.env.E2B_TEMPLATE || undefined;

    const sandbox = await Sandbox.create({
      apiKey,
      timeoutMs: typeof timeoutMs === "number" ? timeoutMs : 5 * 60 * 1000,
      metadata: metadata || {},
      envs: envVars || {},
      ...(tpl ? { template: tpl } : {}),
    });

    console.log(`Sandbox created: ${sandbox.sandboxId} (template: ${tpl || "default"})`);
    res.json({ sandboxId: sandbox.sandboxId });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "Create sandbox failed" });
  }
});

// ─── Run Command ───
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

// ─── Write Files ───
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

// ─── Read File ───
app.post("/sandboxes/:id/files/read", async (req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const sandboxId = req.params.id;
    const { path } = req.body || {};

    if (!path) return res.status(400).json({ error: "Missing path" });

    const sandbox = await Sandbox.connect(sandboxId, { apiKey });
    const content = await sandbox.files.read(path);

    res.json({ ok: true, content: content.toString() });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "Read file failed" });
  }
});

// ─── Get Host URL ───
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

// ─── List Running Sandboxes ───
app.get("/sandboxes", async (_req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const sandboxes = await Sandbox.list({ apiKey });
    res.json({ sandboxes: sandboxes.map(s => ({ sandboxId: s.sandboxId, startedAt: s.startedAt })) });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "List failed" });
  }
});

// ─── Kill Sandbox ───
app.delete("/sandboxes/:id", async (req, res) => {
  try {
    const apiKey = requireEnv("E2B_API_KEY");
    const sandboxId = req.params.id;
    const sandbox = await Sandbox.connect(sandboxId, { apiKey });
    await sandbox.kill();
    console.log(`Sandbox killed: ${sandboxId}`);
    res.json({ ok: true });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e?.message || "Kill failed" });
  }
});

const port = Number(process.env.PORT || 8080);
app.listen(port, () => {
  console.log(`e2b-proxy listening on :${port}`);
  console.log(`E2B_TEMPLATE: ${process.env.E2B_TEMPLATE || "(not set)"}`);
});
