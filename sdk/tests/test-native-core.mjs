#!/usr/bin/env node
import { strict as assert } from "node:assert";
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createY2Agent } from "../node.js";

const scriptDir = fileURLToPath(new URL(".", import.meta.url));
const defaultAddon = resolve(scriptDir, "../../zig-out/lib/liby2.node");
const addon = resolve(process.argv[2] || defaultAddon);
const root = await realpath(await mkdtemp(join(tmpdir(), "liby2-native-sessions-")));
const home = join(root, "home");
const workspaceRoot = join(root, "workspace");
const externalWorkspace = join(root, "external");
const originalCwd = process.cwd();
const events = [];
let agent;

async function seedSession(id, workspace, updatedAtMs) {
  const directory = join(home, ".y2", "sessions", id);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await writeFile(join(directory, "session.json"), JSON.stringify({
    schema_version: 2,
    id,
    created_at_ms: 1,
    updated_at_ms: updatedAtMs,
    workspace_root: workspace,
    conversation_language: "en",
    history_len: 0,
    history: [],
    total_input_tokens: 0,
    total_output_tokens: 0,
  }) + "\n", { mode: 0o600 });
}

try {
  await mkdir(workspaceRoot);
  await mkdir(externalWorkspace);
  const expected = new Set();
  for (let index = 0; index < 101; index += 1) {
    const id = `sdk-session-${String(index).padStart(3, "0")}`;
    expected.add(id);
    await seedSession(id, workspaceRoot, index + 1);
  }
  await seedSession("other-workspace-session", externalWorkspace, 999);

  agent = await createY2Agent({
    nativeAddon: addon,
    backend: "native",
    home,
    workspaceRoot,
    env: { Y2_API_KEY: "native-core-test-key" },
    onEvent(event) { events.push(event); },
  });
  const session = await agent.createSession();
  assert.equal(typeof session.id, "string");
  assert.ok(session.id.length > 0);
  assert.ok(Array.isArray(session.configOptions));
  assert.ok(session.modes);
  const sessions = await agent.listSessions();
  assert.ok(Array.isArray(sessions));
  assert.ok(sessions.length >= 101, "listSessions must exhaust native ACP pages");
  for (const listed of sessions) {
    assert.equal(listed.cwd, workspaceRoot, "session listing must retain native workspace scope");
    expected.delete(listed.sessionId);
  }
  assert.equal(expected.size, 0, "all seeded workspace sessions must remain visible");
  const requests = events.filter((event) => event.type === "acp.send" && event.message.method === "session/list");
  assert.ok(requests.length >= 2);
  assert.ok(requests.every((event) => event.message.params.cwd === workspaceRoot));
  await session.close();
  assert.equal(await agent.close(), 0);
  agent = null;
  assert.ok(events.some((event) => event.type === "runtime.ready"));
  assert.ok(events.some((event) => event.type === "acp.receive"));

  process.chdir(workspaceRoot);
  agent = await createY2Agent({
    nativeAddon: addon,
    backend: "native",
    home,
    env: { Y2_API_KEY: "native-core-test-key" },
  });
  const defaultWorkspaceSessions = await agent.listSessions();
  assert.ok(defaultWorkspaceSessions.length >= 101);
  assert.ok(defaultWorkspaceSessions.every((listed) => listed.cwd === workspaceRoot));
  assert.equal(await agent.close(), 0);
  agent = null;

  process.chdir(root);
  const relativeEvents = [];
  agent = await createY2Agent({
    nativeAddon: addon,
    backend: "native",
    home,
    workspaceRoot: "./workspace",
    env: { Y2_API_KEY: "native-core-test-key" },
    onEvent(event) { relativeEvents.push(event); },
  });
  const relativeWorkspaceSessions = await agent.listSessions();
  assert.ok(relativeWorkspaceSessions.length >= 101);
  assert.ok(relativeWorkspaceSessions.every((listed) => listed.cwd === workspaceRoot));
  const relativeRequests = relativeEvents.filter((event) => event.type === "acp.send" && event.message.method === "session/list");
  assert.ok(relativeRequests.every((event) => event.message.params.cwd === workspaceRoot));
  assert.equal(await agent.close(), 0);
  agent = null;
} finally {
  await agent?.close();
  process.chdir(originalCwd);
  await rm(root, { recursive: true, force: true });
}
console.log("native core passed: session lifecycle, paginated listing, explicit/default/relative workspace scope, and graceful close");
