import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const workflows = Object.fromEntries(
  readdirSync(resolve(root, ".github/workflows"))
    .filter((name) => /\.ya?ml$/.test(name))
    .map((name) => [name, Bun.YAML.parse(readFileSync(resolve(root, ".github/workflows", name), "utf8")) as any]),
);

function policyViolations(items: Record<string, any>): string[] {
  const errors: string[] = [];
  const allowed: Record<string, string[]> = {
    "release.yml": ["push", "workflow_dispatch"],
    "recover-release.yml": ["workflow_dispatch"],
    "publish-liby2.yml": ["workflow_run", "workflow_dispatch"],
    "full-ci.yml": ["workflow_call"],
    "pgso-macos-arm64.yml": ["workflow_call"],
  };
  for (const [name, workflow] of Object.entries(items)) {
    for (const event of Object.keys(workflow.on ?? {})) {
      if (!allowed[name]?.includes(event)) errors.push(`${name}: forbidden ${event} trigger`);
    }
    if (workflow.on?.push && JSON.stringify(workflow.on.push) !== JSON.stringify({ branches: ["main"] })) {
      errors.push(`${name}: pushes must target only main`);
    }
    for (const [id, job] of Object.entries<any>(workflow.jobs)) {
      // Root jobs and always-running aggregates can allocate a runner even
      // when prerequisite work was skipped, so each needs the source guard.
      if (!job.needs || String(job.if).includes("always()")) {
        for (const clause of ["github.repository == 'y2-intel/harness'", "github.ref == 'refs/heads/main'"]) {
          if (!String(job.if).includes(clause)) errors.push(`${name}/${id}: missing ${clause}`);
        }
        if (workflow.on?.workflow_call !== undefined && !String(job.if).includes("github.workflow == 'Release'")) {
          errors.push(`${name}/${id}: reusable work must belong to Release`);
        }
      }
    }
  }
  return errors;
}

describe("main-only release compute policy", () => {
  test("every workflow is confined to canonical main release work", () => {
    expect(policyViolations(workflows)).toEqual([]);
  });

  test("rejects reintroduced PR, feature-push, and unguarded manual work", () => {
    const pr = structuredClone(workflows);
    pr["full-ci.yml"].on.pull_request = {};
    expect(policyViolations(pr).join("\n")).toContain("forbidden pull_request trigger");
    const branch = structuredClone(workflows);
    branch["release.yml"].on.push.branches.push("feature/*");
    expect(policyViolations(branch).join("\n")).toContain("pushes must target only main");
    const manual = structuredClone(workflows);
    delete manual["publish-liby2.yml"].jobs.metadata.if;
    expect(policyViolations(manual).length).toBeGreaterThan(0);
  });

  test("release qualification still covers all four native platforms and E2E shards", () => {
    const full = workflows["full-ci.yml"];
    expect(Object.keys(full.on)).toEqual(["workflow_call"]);
    expect(full.jobs.native.strategy.matrix.platform.map((p: any) => p.runner).sort()).toEqual(
      ["ubuntu-24.04", "ubuntu-24.04-arm", "macos-15-intel", "macos-15"].sort(),
    );
    expect(full.jobs.e2e.strategy.matrix.shard).toHaveLength(4);
    expect(full.jobs["full-suite"].needs).toEqual(["native", "e2e"]);
    const benchmark = full.jobs.native.steps.find((s: any) => s.run?.includes("./benchmarks/startup.sh --ci"));
    expect(benchmark.if).toBe("matrix.platform.name == 'linux-x86_64'");
    const conformance = full.jobs.e2e.steps.filter((s: any) => s["working-directory"] === "tests/e2e/conformance");
    expect(conformance.map((s: any) => s.run)).toEqual(["npm ci", "npm test"]);
    for (const step of conformance) {
      expect(step.if).toBe("matrix.platform.name == 'linux-x86_64' && matrix.shard.index == 0");
    }
  });

  test("packaging and PGSO wait for qualification before allocating runners", () => {
    const release = workflows["release.yml"];
    expect(release.jobs.verify.uses).toBe("./.github/workflows/full-ci.yml");
    for (const name of ["build-linux", "build-macos-x86_64", "build-macos-arm64"]) {
      expect(release.jobs[name].needs).toEqual(["plan", "verify"]);
    }
    expect(release.jobs["build-macos-arm64"].uses).toBe("./.github/workflows/pgso-macos-arm64.yml");
    expect(release.jobs["build-macos-arm64"].with.package_release).toBe(true);
    expect(release.jobs.release.needs).toEqual(["plan", "verify", "build-linux", "build-macos-x86_64", "build-macos-arm64"]);
    expect(release.jobs.release.environment).toBe("release");
  });

  test("SDK publication follows a successful main release and reuses its WASM build for browser tests", () => {
    const sdk = workflows["publish-liby2.yml"];
    expect(sdk.on.workflow_run).toEqual({ workflows: ["Release"], types: ["completed"], branches: ["main"] });
    expect(sdk.jobs.metadata.if).toContain("github.event.workflow_run.conclusion == 'success'");
    expect(sdk.jobs.metadata.if).toContain("github.event.workflow_run.head_branch == 'main'");
    const metadata = sdk.jobs.metadata.steps.find((s: any) => s.id === "metadata").run;
    expect(metadata).toContain('--channel stable');
    expect(metadata).not.toContain('CHANNEL=dev');
    const runs = sdk.jobs.wasm.steps.map((s: any) => s.run ?? "").join("\n");
    expect(runs).toContain("test:node-wasm");
    expect(runs).toContain("test:browser-wasm");
    expect(runs.match(/zig build -Dwasm-surface=core/g)).toHaveLength(1);
    expect(sdk.jobs.publish.environment).toBe("npm");
  });

  test("workflow shell steps parse before spending CI compute", () => {
    for (const [name, workflow] of Object.entries(workflows)) {
      for (const [id, job] of Object.entries<any>(workflow.jobs)) {
        for (const step of job.steps ?? []) {
          if (!step.run) continue;
          const script = step.run.replace(/\$\{\{[\s\S]*?\}\}/g, "placeholder");
          const result = Bun.spawnSync(["bash", "-n"], { stdin: Buffer.from(script), stderr: "pipe" });
          expect(result.exitCode, `${name}/${id}/${step.name}: ${result.stderr}`).toBe(0);
        }
      }
    }
  });
});
