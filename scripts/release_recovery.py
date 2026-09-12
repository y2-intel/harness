"""Recover original release inputs only after their complete CI qualification."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import re
import stat
import subprocess
import zipfile


REPOSITORY = "y2-intel/harness"
PLATFORMS = ("linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-aarch64")
ARTIFACTS = {"release-plan", *(f"y2-{platform}" for platform in PLATFORMS)}


def required_jobs() -> set[str]:
    names = {"plan", "release", *(f"Build {platform}" for platform in PLATFORMS if platform != "macos-aarch64")}
    for platform in PLATFORMS:
        names.add(f"verify / Native checks (ReleaseSafe, {platform})")
        names.add(f"verify / Full suite ({platform})")
        names.update(f"verify / E2E (ReleaseSafe, {platform}, shard {shard}/4)" for shard in range(1, 5))
    names.update(f"build-macos-arm64 / {name}" for name in (
        "Build seed and plan shards", "Merge profiles and build candidate",
        "Aggregate release eligibility", "Package macOS arm64 CLI release",
        *(f"Startup {command}" for command in ("help", "version", "status", "doctor", "sessions", "background")),
        *(f"Heavy {case}" for case in ("approval-transcript", "approval-diff", "approval-payload", "approval-combined", "file-index-100k", "ui-activity")),
    ))
    return names


def validate_qualification(run_id: int, run: dict, jobs: list[dict], artifacts: list[dict]) -> dict[str, dict]:
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("recovery requires a positive release run ID")
    if (
        run.get("id") != run_id or run.get("path") != ".github/workflows/release.yml"
        or run.get("event") not in ("push", "workflow_dispatch") or run.get("head_branch") != "main"
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("status") != "completed" or run.get("conclusion") not in ("success", "failure")
        or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))
    ):
        raise ValueError("recovery requires a completed canonical main Release run")
    names = [job.get("name") for job in jobs]
    if len(names) != len(set(names)) or not required_jobs().issubset(names):
        raise ValueError("release qualification has missing or duplicate jobs")
    for prefix in ("build-macos-arm64 / Train training-", "build-macos-arm64 / Verify behavior-"):
        if not any(name.startswith(prefix) for name in names):
            raise ValueError("release qualification is missing PGSO shards")
    for job in jobs:
        allowed = ("success", "failure") if job["name"] == "release" else ("success",)
        if (job.get("run_id") != run_id or job.get("head_sha") != run["head_sha"]
                or job.get("status") != "completed" or job.get("conclusion") not in allowed):
            raise ValueError(f"release qualification did not pass: {job['name']}")
    selected = {}
    for artifact in artifacts:
        name = artifact.get("name", "")
        if name not in ARTIFACTS:
            continue
        origin = artifact.get("workflow_run", {})
        if (name in selected or artifact.get("expired") is not False
                or type(artifact.get("id")) is not int or artifact["id"] <= 0
                or type(artifact.get("size_in_bytes")) is not int or not 0 < artifact["size_in_bytes"] <= 128 * 1024 * 1024
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", artifact.get("digest", ""))
                or origin.get("id") != run_id or origin.get("head_sha") != run["head_sha"]
                or origin.get("head_branch") != "main"
                or origin.get("repository_id") != run["repository"]["id"]
                or origin.get("head_repository_id") != run["repository"]["id"]):
            raise ValueError(f"invalid qualified artifact: {name}")
        selected[name] = artifact
    if set(selected) != ARTIFACTS:
        raise ValueError("original release artifacts are missing")
    return selected


def api(path: str, *, paginate: bool = False) -> object:
    args = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
    if paginate:
        args += ["--paginate", "--slurp"]
    return json.loads(subprocess.check_output(args))


def extract_artifact(artifact: dict, data: bytes, destination: pathlib.Path) -> None:
    if len(data) != artifact["size_in_bytes"] or "sha256:" + hashlib.sha256(data).hexdigest() != artifact["digest"]:
        raise ValueError("downloaded artifact does not match its immutable digest")
    name = artifact["name"]
    expected = {"release-plan.json"} if name == "release-plan" else {name + ".tar.gz", name + ".tar.gz.sha256"}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) != len(expected) or {member.filename for member in members} != expected:
            raise ValueError("unexpected release artifact paths")
        for member in members:
            mode = stat.S_IFMT(member.external_attr >> 16)
            if member.is_dir() or mode not in (0, stat.S_IFREG) or member.file_size > 128 * 1024 * 1024:
                raise ValueError("invalid release artifact member")
            with (destination / member.filename).open("xb") as output:
                output.write(archive.read(member))


def download_qualified_inputs(run_id: int, destination: pathlib.Path) -> dict:
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("recovery requires a positive release run ID")
    run = api(f"actions/runs/{run_id}")
    jobs = [job for page in api(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100", paginate=True) for job in page["jobs"]]
    artifacts = [artifact for page in api(f"actions/runs/{run_id}/artifacts?per_page=100", paginate=True) for artifact in page["artifacts"]]
    selected = validate_qualification(run_id, run, jobs, artifacts)
    assets = destination / "assets"
    assets.mkdir(parents=True, exist_ok=False)
    for name, artifact in sorted(selected.items()):
        data = subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"])
        extract_artifact(artifact, data, destination if name == "release-plan" else assets)
    plan = json.loads((destination / "release-plan.json").read_text())
    if (plan.get("schema_version") != 1 or plan.get("needed") is not True
            or plan.get("source_sha") != run["head_sha"]
            or not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", plan.get("tag", ""))
            or plan.get("version") != plan["tag"][1:]):
        raise ValueError("original plan does not match the qualified release source")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    plan = download_qualified_inputs(args.run_id, args.output)
    print(f"Recovered qualified inputs for {plan['tag']} at {plan['source_sha']} from run {args.run_id}")


if __name__ == "__main__":
    main()
