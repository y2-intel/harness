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


def validate_publish_source(source: str, tag: str) -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_SHA") != source
    ):
        raise ValueError("release publication requires canonical main CI at the exact plan source")
    if run("git", "rev-parse", "HEAD").decode().strip() != source:
        raise ValueError("release plan does not match the checked-out source")
    run("git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
    history = run("git", "rev-list", "--first-parent", "refs/remotes/origin/main").decode().splitlines()
    if source not in history:
        raise ValueError("release source is not on the current origin/main first-parent lineage")
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


def publish(plan: dict, assets: pathlib.Path, notes: pathlib.Path) -> None:
    tag, source = plan["tag"], plan["source_sha"]
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("invalid release tag")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("release plan requires a full source SHA")
    validate_publish_source(source, tag)
    digests = verify_assets(assets)
    reference = optional_api(f"git/ref/tags/{tag}")
    if reference is None:
        api("git/refs", payload={"ref": f"refs/tags/{tag}", "sha": source})
    else:
        obj = reference["object"]
        while obj["type"] == "tag":
            obj = api(f"git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit" or obj["sha"] != source:
            raise ValueError("release tag points to different source; never retag")
    release = optional_api(f"releases/tags/{tag}")
    if release is None:
        run("gh", "release", "create", tag, "--repo", REPOSITORY, "--verify-tag", "--draft", "--title", tag, "--notes-file", str(notes))
        release = api(f"releases/tags/{tag}")
    if release.get("prerelease"):
        raise ValueError("stable release tag is already a prerelease")
    release = remove_empty_draft_uploads(release)
    existing = validate_remote_assets(release, digests, assets)
    missing = sorted(expected_assets() - existing)
    if missing:
        run("gh", "release", "upload", tag, "--repo", REPOSITORY, *(str(assets / name) for name in missing))
    release = api(f"releases/tags/{tag}")
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
    args = parser.parse_args()
    publish(json.loads(args.plan.read_text()), args.assets, args.notes)


if __name__ == "__main__":
    main()
