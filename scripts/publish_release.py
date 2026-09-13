"""Publish a fully verified draft from the immutable CI release plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tarfile
import tempfile

if __package__:
    from . import release_recovery
else:
    import release_recovery


REPOSITORY = "y2-intel/harness"
PLATFORMS = ("linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-aarch64")


def expected_assets() -> set[str]:
    return {f"y2-{platform}.tar.gz{suffix}" for platform in PLATFORMS for suffix in ("", ".sha256")}


def verify_assets(directory: pathlib.Path) -> dict[str, str]:
    files = {path.name for path in directory.iterdir() if path.is_file()}
    if files != expected_assets():
        raise ValueError("release requires exactly four platform archives and their checksums")
    digests = {}
    for name in sorted(files):
        path = directory / name
        if path.is_symlink() or path.stat().st_size == 0:
            raise ValueError(f"invalid release asset: {name}")
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if name.endswith(".sha256"):
            archive = name.removesuffix(".sha256")
            match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^\r\n]+)\n?", path.read_text())
            if not match or match[2] != archive:
                raise ValueError(f"invalid checksum sidecar: {name}")
            actual = hashlib.sha256((directory / archive).read_bytes()).hexdigest()
            if match[1].lower() != actual:
                raise ValueError(f"archive checksum mismatch: {archive}")
        else:
            with tarfile.open(path, "r:gz") as archive:
                members = archive.getmembers()
                if {m.name for m in members} != {"y2", "LICENSE", "THIRD_PARTY_NOTICES.md"} or len(members) != 3:
                    raise ValueError(f"unexpected archive contents: {name}")
                if any(not member.isfile() for member in members):
                    raise ValueError(f"non-regular archive member: {name}")
    return digests


def run(*args: str) -> bytes:
    return subprocess.check_output(args)


def api(path: str, *, payload: dict | None = None) -> object:
    command = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
    if payload is None:
        return json.loads(run(*command))
    command += ["--method", "POST", "--input", "-"]
    return json.loads(subprocess.check_output(command, input=json.dumps(payload).encode()))


def optional_api(path: str) -> dict | None:
    result = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/{path}"], capture_output=True)
    if result.returncode == 0:
        return json.loads(result.stdout)
    try:
        error = json.loads(result.stdout)
    except ValueError:
        error = {}
    if error.get("status") in (404, "404"):
        return None
    raise RuntimeError(f"could not inspect {path}: {result.stderr.decode().strip()}")


def find_release(tag: str) -> dict | None:
    # GitHub's release-by-tag endpoint excludes unpublished drafts.
    pages = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases?per_page=100", "--paginate", "--slurp"))
    matches = [release for page in pages for release in page if release.get("tag_name") == tag]
    if len(matches) > 1:
        raise ValueError(f"multiple releases use the planned tag: {tag}")
    return matches[0] if matches else None


def refresh_release(release: dict, tag: str) -> dict:
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("release is missing a valid numeric ID")
    current = api(f"releases/{release_id}")
    if current.get("id") != release_id or current.get("tag_name") != tag:
        raise ValueError("release identity changed during publication")
    return current


def validate_remote_assets(release: dict, digests: dict[str, str], directory: pathlib.Path) -> set[str]:
    existing = set()
    for asset in release.get("assets", []):
        name = asset["name"]
        if name not in digests or name in existing:
            raise ValueError(f"unexpected or duplicate remote asset: {name}")
        if asset["state"] != "uploaded" or asset["size"] != (directory / name).stat().st_size:
            raise ValueError(f"incomplete or conflicting remote asset: {name}")
        digest = asset.get("digest")
        if not digest:
            data = run("gh", "api", f"repos/{REPOSITORY}/releases/assets/{asset['id']}", "-H", "Accept: application/octet-stream")
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if digest != "sha256:" + digests[name]:
            raise ValueError(f"refusing to replace different bytes for {name}")
        existing.add(name)
    return existing


def remove_empty_draft_uploads(release: dict) -> dict:
    if release.get("draft") is not True:
        return release
    retained = []
    for asset in release.get("assets", []):
        if (
            asset.get("name") in expected_assets()
            and asset.get("state") == "starter"
            and type(asset.get("size")) is int
            and asset["size"] == 0
        ):
            if type(asset.get("id")) is not int or asset["id"] <= 0:
                raise ValueError("empty draft upload is missing a valid asset ID")
            run("gh", "api", f"repos/{REPOSITORY}/releases/assets/{asset['id']}", "--method", "DELETE")
        else:
            retained.append(asset)
    return {**release, "assets": retained}


def tag_commit(tag: str) -> str:
    reference = optional_api(f"git/ref/tags/{tag}")
    if reference is None:
        raise ValueError(f"published release tag is missing: {tag}")
    obj = reference["object"]
    while obj["type"] == "tag":
        obj = api(f"git/tags/{obj['sha']}")["object"]
    if obj["type"] != "commit" or not re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
        raise ValueError(f"release tag does not resolve to a commit: {tag}")
    return obj["sha"]


def validate_publish_source(source: str, tag: str, *, qualified_run: int | None = None) -> None:
    delivery_source = os.environ.get("GITHUB_SHA")
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or not re.fullmatch(r"[0-9a-f]{40}", delivery_source or "")
        or (qualified_run is None and delivery_source != source)
    ):
        raise ValueError("release publication requires canonical main CI at the exact plan source")
    if qualified_run is not None and (
        type(qualified_run) is not int or qualified_run <= 0
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_WORKFLOW_REF") != f"{REPOSITORY}/.github/workflows/recover-release.yml@refs/heads/main"
    ):
        raise ValueError("qualified recovery requires the canonical main recovery workflow")
    if run("git", "rev-parse", "HEAD").decode().strip() != delivery_source:
        raise ValueError("release plan does not match the checked-out source")
    run("git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
    history = run("git", "rev-list", "--first-parent", "refs/remotes/origin/main").decode().splitlines()
    if source not in history:
        raise ValueError("release source is not on the current origin/main first-parent lineage")
    if delivery_source not in history or history.index(delivery_source) > history.index(source):
        raise ValueError("delivery source must follow the release on current origin/main")
    source_ancestors = set(history[history.index(source):])
    main_ancestors = set(history)
    pages = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases?per_page=100", "--paginate", "--slurp"))
    canonical = []
    for page in pages:
        for release in page:
            name = release.get("tag_name", "")
            if release.get("draft") is not False or release.get("prerelease") is not False:
                continue
            if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", name):
                continue
            target = tag_commit(name)
            if target in main_ancestors:
                canonical.append((tuple(map(int, name[1:].split("."))), name, target))
    if not canonical:
        raise ValueError("no published stable Y2 release anchors the current main lineage")
    version, latest_tag, latest_source = max(canonical)
    if latest_source not in source_ancestors:
        raise ValueError(f"release source predates published release {latest_tag}; refusing out-of-order publication")
    if tuple(map(int, tag[1:].split("."))) < version:
        raise ValueError("refusing to publish a version below the latest Y2 release")


def publish(plan: dict, assets: pathlib.Path, notes: pathlib.Path, *, qualified_run: int | None = None) -> None:
    tag, source = plan["tag"], plan["source_sha"]
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("invalid release tag")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("release plan requires a full source SHA")
    validate_publish_source(source, tag, qualified_run=qualified_run)
    digests = verify_assets(assets)
    if qualified_run is not None:
        # Revalidate immediately before publication; workflow inputs are not proof.
        with tempfile.TemporaryDirectory(prefix="y2-qualified-release-") as temp:
            original = pathlib.Path(temp)
            original_plan = release_recovery.download_qualified_inputs(qualified_run, original)
            if plan != original_plan or digests != verify_assets(original / "assets"):
                raise ValueError("recovery must reuse the exact qualified plan and artifact bytes")
    reference = optional_api(f"git/ref/tags/{tag}")
    if reference is None:
        if qualified_run is not None:
            raise ValueError("recovery requires an existing immutable release tag")
        api("git/refs", payload={"ref": f"refs/tags/{tag}", "sha": source})
    else:
        obj = reference["object"]
        while obj["type"] == "tag":
            obj = api(f"git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit" or obj["sha"] != source:
            raise ValueError("release tag points to different source; never retag")
    release = find_release(tag)
    if release is None:
        if qualified_run is not None:
            raise ValueError("recovery requires an existing release draft")
        # Use the created resource directly; the release list can lag behind a successful write.
        release = api("releases", payload={
            "tag_name": tag, "target_commitish": source, "name": tag,
            "draft": True, "prerelease": False, "body": notes.read_text(),
        })
        if not isinstance(release, dict) or release.get("tag_name") != tag or release.get("draft") is not True or release.get("prerelease") is not False:
            raise ValueError("created release does not match the planned draft")
    release = refresh_release(release, tag)
    if qualified_run is not None and release.get("draft") is not True:
        raise ValueError("recovery requires an unpublished release draft")
    if release.get("prerelease"):
        raise ValueError("stable release tag is already a prerelease")
    release = remove_empty_draft_uploads(release)
    existing = validate_remote_assets(release, digests, assets)
    missing = sorted(expected_assets() - existing)
    if missing:
        run("gh", "release", "upload", tag, "--repo", REPOSITORY, *(str(assets / name) for name in missing))
    release = refresh_release(release, tag)
    if validate_remote_assets(release, digests, assets) != expected_assets():
        raise ValueError("release assets are incomplete")
    latest = optional_api("releases/latest")
    if latest and re.fullmatch(r"v\d+\.\d+\.\d+", latest["tag_name"]):
        numeric = lambda value: tuple(map(int, value[1:].split(".")))
        if numeric(latest["tag_name"]) > numeric(tag):
            raise ValueError("refusing to move latest backwards")
    run("gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest", "--notes-file", str(notes))
    print(f"Published {tag} at {source} with all eight verified assets")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=pathlib.Path, required=True)
    parser.add_argument("--assets", type=pathlib.Path, required=True)
    parser.add_argument("--notes", type=pathlib.Path, required=True)
    parser.add_argument("--qualified-run", type=int, help="Recover original artifacts from a fully qualified Release run")
    args = parser.parse_args()
    publish(json.loads(args.plan.read_text()), args.assets, args.notes, qualified_run=args.qualified_run)


if __name__ == "__main__":
    main()
