import { describe, expect, test } from "bun:test";
import { existsSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import {
  buildEvalArgs,
  buildEvalProcessEnv,
  runEval,
  shouldLoadDotEnv,
  Y2_BIN,
} from "./eval-helpers";

function evalHomes(): Set<string> {
  return new Set(
    readdirSync(tmpdir()).filter((name) => name.startsWith("y2-eval-home-")),
  );
}

describe("eval helpers", () => {
  test("passes the selected eval model to y2 through Y2_MODEL", () => {
    const previous = process.env.Y2_MODEL;
    process.env.Y2_MODEL = "ambient/model";

    try {
      const env = buildEvalProcessEnv("/tmp/y2-eval-home-test", "selected/model");

      expect(env.Y2_MODEL).toBe("selected/model");
      expect(env.HOME).toBe("/tmp/y2-eval-home-test");
      expect(env.NO_COLOR).toBe("1");
    } finally {
      if (previous === undefined) {
        delete process.env.Y2_MODEL;
      } else {
        process.env.Y2_MODEL = previous;
      }
    }
  });

  test("passes the eval budget to y2 --timeout in seconds", () => {
    const args = buildEvalArgs("hello", 150);
    const flag = args.indexOf("--timeout");

    expect(flag).toBeGreaterThan(-1);
    expect(args[flag + 1]).toBe("150");
    expect(args.at(-1)).toBe("hello");
  });

  test.skipIf(!existsSync(Y2_BIN))(
    "stops y2 when the eval budget runs out and removes its home",
    async () => {
      let modelRequests = 0;
      const gateway = Bun.serve({
        port: 0,
        idleTimeout: 0,
        fetch(req) {
          if (req.method === "GET") {
            return Response.json({ data: [{ id: "fake/model" }] });
          }
          modelRequests += 1;
          return new Promise<Response>(() => {});
        },
      });
      const overrides: Record<string, string> = {
        Y2_API_KEY: "",
        OPENAI_API_KEY: "fake-key",
        OPENAI_BASE_URL: `http://127.0.0.1:${gateway.port}`,
        Y2_API_CHAT_URL: `http://127.0.0.1:${gateway.port}/v1/chat/completions`,
        Y2_AUTO_UPGRADE: "0",
      };
      const previous = Object.fromEntries(
        Object.keys(overrides).map((key) => [key, process.env[key]]),
      );
      Object.assign(process.env, overrides);
      const homesBefore = evalHomes();
      // Bun's 15s test timeout did not end this test when runEval never
      // stopped y2, so the test keeps its own deadline.
      let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
      const deadline = new Promise<never>((_, reject) => {
        deadlineTimer = setTimeout(
          () => reject(new Error("runEval did not stop y2 within 10s")),
          10_000,
        );
      });

      try {
        const failure = await Promise.race([
          runEval("hello", { model: "fake/model", timeoutSec: 1 })
            .then(() => new Error("runEval unexpectedly succeeded"))
            .catch((error: unknown) => error),
          deadline,
        ]);
        expect(failure).toBeInstanceOf(Error);
        expect((failure as Error).message).toBe(
          "y2 ask did not finish within 1s\nstderr: ",
        );
        expect(modelRequests).toBeGreaterThan(0);
        expect([...evalHomes()].filter((name) => !homesBefore.has(name))).toEqual([]);
      } finally {
        clearTimeout(deadlineTimer);
        for (const [key, value] of Object.entries(previous)) {
          if (value === undefined) {
            delete process.env[key];
          } else {
            process.env[key] = value;
          }
        }
        gateway.stop(true);
      }
    },
    15_000,
  );

  test("does not load repository dotenv files in a hermetic run", () => {
    expect(shouldLoadDotEnv({ Y2_E2E_DISABLE_DOTENV: "1" })).toBe(false);
    expect(shouldLoadDotEnv({})).toBe(true);
  });
});
