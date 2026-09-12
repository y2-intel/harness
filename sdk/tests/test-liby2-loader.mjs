#!/usr/bin/env node
import { strict as assert } from "node:assert";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
  createY2Agent,
  createY2Terminal,
  y2SdkApiVersion,
  liby2ApiVersion,
} from "../node.js";
import * as browser from "../browser.js";
import { createY2Agent as createCoreAgent } from "../y2-sdk.js";

assert.equal(liby2ApiVersion, 2);
assert.equal(y2SdkApiVersion, 1);
assert.equal(browser.liby2ApiVersion, 2);
assert.equal(typeof browser.createY2Agent, "function");
assert.equal(typeof browser.createY2Terminal, "function");

const dir = await mkdtemp(resolve(tmpdir(), "liby2-loader-"));
const nativePath = resolve(dir, "native.mjs");
await writeFile(nativePath, `
  export async function createY2Agent(options) { return { backend: "native-agent", options }; }
  export async function createY2Terminal(options) { return { backend: "native-terminal", options }; }
`);
const nativeUrl = pathToFileURL(nativePath);

for (const gatewayChatUrl of [
  "http://attacker.example/chat",
  "https://[redacted]@example.com/chat",
  "file:///tmp/socket",
]) {
  await assert.rejects(
    createY2Agent({ nativeAddon: nativeUrl, env: { Y2_API_CHAT_URL: gatewayChatUrl } }),
    TypeError,
  );
}

const directAgent = await createY2Agent({
  nativeAddon: nativeUrl,
  env: {
    OPENAI_BASE_URL: "https://models.example/v1",
    OPENAI_API_KEY: "direct-test-key",
  },
});
assert.equal(directAgent.backend, "native-agent");
assert.equal(directAgent.options.env.OPENAI_BASE_URL, "https://models.example/v1");

const agent = await createY2Agent({ nativeAddon: nativeUrl, marker: 1 });
assert.equal(agent.backend, "native-agent");
assert.equal(agent.options.marker, 1);
assert.equal("nativeAddon" in agent.options, false);
assert.equal("backend" in agent.options, false);

const terminal = await createY2Terminal({ nativeAddon: nativeUrl, marker: 2 });
assert.equal(terminal.backend, "native-terminal");
assert.equal(terminal.options.marker, 2);

await assert.rejects(
  createY2Agent({ nativeAddon: nativeUrl, backend: "wasm" }),
  (error) => error?.code === "LIBY2_JSPI_REQUIRED" &&
    error.message.includes("--experimental-wasm-jspi"),
);

const coreOnlyPath = resolve(dir, "core-only.mjs");
await writeFile(coreOnlyPath, `
  export const liby2ApiVersion = 2;
  export async function createY2Agent() { return { backend: "core-only" }; }
`);
await assert.rejects(
  createY2Terminal({ nativeAddon: pathToFileURL(coreOnlyPath), backend: "native" }),
  (error) => error?.code === "LIBY2_NATIVE_UNAVAILABLE" &&
    error.message.includes("createY2Terminal"),
);

const incompatiblePath = resolve(dir, "incompatible.mjs");
await writeFile(incompatiblePath, `
  export const liby2ApiVersion = 3;
  export async function createY2Agent() {}
`);
await assert.rejects(
  createY2Agent({ nativeAddon: pathToFileURL(incompatiblePath), backend: "native" }),
  (error) => error?.code === "LIBY2_NATIVE_UNAVAILABLE" &&
    error.message.includes("incompatible"),
);

for (const [name, source] of [
  ["missing-version", `
    export function createCore() { throw new Error("missing-version createCore invoked"); }
  `],
  ["unequal-version", `
    export const liby2ApiVersion = 3;
    export function createCore() { throw new Error("unequal-version createCore invoked"); }
  `],
]) {
  const modulePath = resolve(dir, `${name}.mjs`);
  await writeFile(modulePath, source);
  await assert.rejects(
    createY2Agent({ nativeAddon: pathToFileURL(modulePath), backend: "native" }),
    (error) => error?.code === "LIBY2_NATIVE_UNAVAILABLE" &&
      error.message.includes("incompatible") &&
      !String(error.cause).includes("createCore invoked"),
    `${name} low-level addon must fail before createCore invocation`,
  );
}

const matchingVersionPath = resolve(dir, "matching-version.mjs");
await writeFile(matchingVersionPath, `
  export const liby2ApiVersion = 2;
  export function createCore() {
    const error = new Error("matching-version createCore invoked");
    error.code = "MATCHING_VERSION_INVOKED";
    throw error;
  }
`);
await assert.rejects(
  createY2Agent({ nativeAddon: pathToFileURL(matchingVersionPath), backend: "native" }),
  (error) => error?.code === "MATCHING_VERSION_INVOKED",
  "matching v2 low-level addon must reach createCore",
);


async function listWithRuntimePages(pages, workspaceRoot) {
  const requests = [];
  const events = [];
  let handler;
  let finish;
  let pageIndex = 0;
  const exited = new Promise((resolve) => { finish = resolve; });
  const runtime = {
    exited,
    setLineHandler(value) { handler = value; },
    write(line) {
      const message = JSON.parse(line);
      const result = message.method === "session/list"
        ? pages[pageIndex++]
        : { protocolVersion: 1 };
      if (message.method === "session/list") requests.push(message.params);
      queueMicrotask(() => handler({ jsonrpc: "2.0", id: message.id, result }));
    },
    closeStdin() { finish(0); },
    abort() { finish(0); },
    abortHostEffects() {},
  };
  const coreAgent = await createCoreAgent({
    ...(workspaceRoot === undefined ? {} : { workspaceRoot }),
    runtimeFactory: async () => runtime,
    onEvent(event) {
      if (event.type === "acp.send" && event.message.method === "session/list") events.push(event);
    },
  });
  return { agent: coreAgent, requests, events };
}

const firstPage = Array.from({ length: 100 }, (_, index) => ({ sessionId: `session-${index}` }));
const lastSession = { sessionId: "session-100" };
const paginated = await listWithRuntimePages([
  { sessions: firstPage, nextCursor: "page-2" },
  { sessions: [], nextCursor: "page-3" },
  { sessions: [lastSession] },
], "/workspace/sdk");
try {
  assert.deepEqual(await paginated.agent.listSessions(), [...firstPage, lastSession]);
  assert.deepEqual(paginated.requests, [
    { cwd: "/workspace/sdk" },
    { cwd: "/workspace/sdk", cursor: "page-2" },
    { cwd: "/workspace/sdk", cursor: "page-3" },
  ]);
  assert.deepEqual(
    paginated.events.map((event) => event.message.params),
    paginated.requests,
    "later cursor requests must not mutate earlier acp.send events",
  );
} finally { await paginated.agent.close(); }

const hostStore = await listWithRuntimePages([{ sessions: [lastSession] }]);
try {
  assert.deepEqual(await hostStore.agent.listSessions(), [lastSession]);
  assert.deepEqual(hostStore.requests, [{}], "host-backed sessions retain an unscoped list request");
} finally { await hostStore.agent.close(); }

const repeatedCursor = await listWithRuntimePages([
  { sessions: firstPage, nextCursor: "same-page" },
  { sessions: [], nextCursor: "same-page" },
]);
try {
  await assert.rejects(repeatedCursor.agent.listSessions(), /repeated session-list cursor/);
  assert.equal(repeatedCursor.requests.length, 2, "a repeated cursor must stop pagination");
} finally { await repeatedCursor.agent.close(); }

const invalidCursor = await listWithRuntimePages([{ sessions: [], nextCursor: 4 }]);
try {
  await assert.rejects(invalidCursor.agent.listSessions(), /invalid session-list cursor/);
} finally { await invalidCursor.agent.close(); }

console.log("liby2 loader passed: browser exports, native preference, fallback diagnostics, strict low-level API validation, and compatible session pagination");
