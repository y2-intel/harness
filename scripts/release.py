#!/usr/bin/env python3
"""Plan immutable Y2 releases without modifying Git or GitHub state."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REPOSITORY = "y2-intel/harness"
MAX_VERSION_PART = (1 << 32) - 1
MAX_VERSION_LENGTH = 32
TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
BUMP_PATTERN = re.compile(r"(?<![\w#])#(major|minor)(?![\w-])", re.IGNORECASE)
PLATFORMS = ("linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-aarch64")
EXPECTED_ASSETS = frozenset(
    f"y2-{platform}.tar.gz{suffix}"
    for platform in PLATFORMS
    for suffix in ("", ".sha256")
)


class ReleaseError(ValueError):
    pass


def parse_version(tag: str) -> tuple[int, int, int] | None:
    if len(tag) > MAX_VERSION_LENGTH + 1:
        return None
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        return None
    version = tuple(map(int, match.groups()))
    return version if all(part <= MAX_VERSION_PART for part in version) else None


def require_sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise ReleaseError("source and tag targets must be full lowercase Git commit SHAs")
    return value


def published_stable(release: dict[str, Any]) -> bool:
    return release.get("draft") is False and release.get("prerelease") is False


def complete_release(release: dict[str, Any]) -> bool:
    if not published_stable(release):
        return False
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise ReleaseError("release assets must be a list")
    for name in EXPECTED_ASSETS:
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
        if len(matches) != 1:
            return False
        asset = matches[0]
        if asset.get("state") != "uploaded" or type(asset.get("size")) is not int or asset["size"] <= 0:
            return False
    return True


def release_context(payload: dict[str, Any]) -> tuple[str, list[dict[str, str]], dict[str, str], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ReleaseError("release input must be a JSON object")
    source = require_sha(payload.get("source_sha"))
    history = payload.get("first_parent")
    if not isinstance(history, list) or not history:
        raise ReleaseError("first_parent must contain the pinned source commit and its ancestors")
    seen = set()
    for commit in history:
        if not isinstance(commit, dict):
            raise ReleaseError("first_parent entries must contain sha and message")
        sha = require_sha(commit.get("sha"))
        if sha in seen or not isinstance(commit.get("message"), str):
            raise ReleaseError("first_parent contains duplicate commits or invalid messages")
        seen.add(sha)
    if history[0]["sha"] != source:
        raise ReleaseError("first_parent must begin at the pinned source SHA")
    tags = payload.get("tags")
    if not isinstance(tags, dict):
        raise ReleaseError("tags must map remote tag names to peeled commit SHAs")
    for name, sha in tags.items():
        if not isinstance(name, str):
            raise ReleaseError("tag names must be strings")
        require_sha(sha)
    releases = payload.get("releases")
    if not isinstance(releases, list):
        raise ReleaseError("releases must be a list")
    by_tag = {}
    for release in releases:
        if not isinstance(release, dict):
            raise ReleaseError("release entries must be objects")
        tag = release.get("tag_name")
        if not isinstance(tag, str) or parse_version(tag) is None:
            continue
        if type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
            raise ReleaseError("release publication flags must be booleans")
        if tag in by_tag:
            raise ReleaseError(f"duplicate release metadata for {tag}")
        if published_stable(release) and tag not in tags:
            raise ReleaseError(f"published release {tag} is missing its immutable tag")
        by_tag[tag] = release
    return source, history, tags, by_tag


def canonical_releases(history: list[dict[str, str]], tags: dict[str, str], releases: dict[str, dict[str, Any]]) -> list[str]:
    ancestors = {commit["sha"] for commit in history}
    return sorted(
        (tag for tag, release in releases.items() if published_stable(release) and tags.get(tag) in ancestors),
        key=lambda tag: parse_version(tag),
        reverse=True,
    )


def bump_version(version: tuple[int, int, int], messages: list[str]) -> tuple[tuple[int, int, int], str]:
    if len(version) != 3 or any(type(part) is not int or not 0 <= part <= MAX_VERSION_PART for part in version):
        raise ReleaseError("version components must fit unsigned 32-bit integers")
    markers = {match.group(1).lower() for message in messages for match in BUMP_PATTERN.finditer(message)}
    major, minor, patch = version
    if "major" in markers:
        result, bump = (major + 1, 0, 0), "major"
    elif "minor" in markers:
        result, bump = (major, minor + 1, 0), "minor"
    else:
        result, bump = (major, minor, patch + 1), "patch"
    if parse_version("v" + ".".join(map(str, result))) is None:
        raise ReleaseError("version bump exceeds the updater's 32-byte, unsigned 32-bit SemVer limits")
    return result, bump


def result_payload(source: str, tag: str, previous_tag: str, tags: dict[str, str], releases: dict[str, dict[str, Any]], *, bump: str) -> dict[str, Any]:
    release = releases.get(tag)
    return {
        "schema_version": 1,
        "version": tag[1:],
        "tag": tag,
        "previous_tag": previous_tag,
        "source_sha": source,
        "needed": release is None or not complete_release(release),
        "tag_exists": tag in tags,
        "release_exists": release is not None,
        "bump": bump,
    }


def plan_release(payload: dict[str, Any]) -> dict[str, Any]:
    source, history, tags, releases = release_context(payload)
    canonical = canonical_releases(history, tags, releases)
    if not canonical:
        raise ReleaseError("no published stable Y2 release exists on the source first-parent lineage; establish an explicit release baseline")

    exact_published = [tag for tag in canonical if tags[tag] == source]
    if len(exact_published) > 1:
        raise ReleaseError("multiple published stable versions target the source SHA")
    if exact_published:
        tag = exact_published[0]
        previous = next((prior for prior in canonical if parse_version(prior) < parse_version(tag)), "")
        return result_payload(source, tag, previous, tags, releases, bump="reuse")

    baseline = canonical[0]
    baseline_version = parse_version(baseline)
    exact_tags = [tag for tag, sha in tags.items() if sha == source and parse_version(tag) is not None]
    if len(exact_tags) > 1:
        raise ReleaseError("multiple SemVer tags target the source SHA; cannot choose a release reservation")
    if exact_tags:
        tag = exact_tags[0]
        if parse_version(tag) <= baseline_version:
            raise ReleaseError(f"reserved tag {tag} does not advance published release {baseline}")
        if tag in releases and releases[tag].get("prerelease"):
            raise ReleaseError(f"reserved tag {tag} belongs to a prerelease")
        return result_payload(source, tag, baseline, tags, releases, bump="reuse")

    messages = []
    for commit in history:
        if commit["sha"] == tags[baseline]:
            break
        messages.append(commit["message"])
    version, bump = bump_version(baseline_version, messages)
    tag = "v" + ".".join(map(str, version))
    if tag in tags and tags[tag] != source:
        raise ReleaseError(f"tag collision: {tag} already targets {tags[tag]}, not {source}; refusing to retag")
    if tag in releases:
        raise ReleaseError(f"release {tag} already exists without a matching source tag")
    return result_payload(source, tag, baseline, tags, releases, bump=bump)


def resolve_metadata(payload: dict[str, Any], channel: str) -> dict[str, Any]:
    source, history, tags, releases = release_context(payload)
    canonical = canonical_releases(history, tags, releases)
    if not canonical:
        raise ReleaseError("no published stable Y2 release exists on the source first-parent lineage")
    if channel == "stable":
        matching = [tag for tag in canonical if tags[tag] == source and complete_release(releases[tag])]
        if len(matching) != 1:
            raise ReleaseError("stable metadata requires one complete published release at the exact source SHA")
        tag = matching[0]
    elif channel == "dev":
        tag = canonical[0]
    else:
        raise ReleaseError("metadata channel must be stable or dev")
    previous = next((prior for prior in canonical if parse_version(prior) < parse_version(tag)), "")
    result = result_payload(source, tag, previous, tags, releases, bump="metadata")
    result["channel"] = channel
    return result


def run_readonly(argv: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    except OSError as error:
        raise ReleaseError(f"could not execute {argv[0]}") from error
    if result.returncode != 0:
        # Authentication failures can include credential-bearing remote URLs.
        raise ReleaseError(f"{argv[0]} read failed with exit status {result.returncode}")
    return result.stdout


def live_payload(source: str, cwd: Path) -> dict[str, Any]:
    source = require_sha(source)
    resolved = run_readonly(["git", "rev-parse", "--verify", f"{source}^{{commit}}"], cwd).strip()
    if resolved != source:
        raise ReleaseError("checkout cannot resolve the pinned source commit")
    remote = run_readonly(["git", "remote", "get-url", "origin"], cwd).strip().removesuffix(".git")
    if remote not in (f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}", f"ssh://git@github.com/{REPOSITORY}"):
        raise ReleaseError("origin must point to the canonical y2-intel/harness repository")
    raw_tags = run_readonly(["git", "ls-remote", "--tags", "origin"], cwd)
    tags = {}
    peeled = {}
    for line in raw_tags.splitlines():
        sha, ref = line.split("\t", 1)
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            tags[name] = sha
    tags.update(peeled)
    raw_history = run_readonly(["git", "log", "--first-parent", "--format=%H%x00%B%x00", source], cwd)
    fields = raw_history.split("\0")
    if fields[-1].strip() == "":
        fields.pop()
    if len(fields) % 2:
        raise ReleaseError("Git returned malformed first-parent history")
    history = [{"sha": fields[index].strip(), "message": fields[index + 1]} for index in range(0, len(fields), 2)]
    pages = json.loads(run_readonly(["gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/releases?per_page=100"], cwd))
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise ReleaseError("GitHub returned malformed release pages")
    return {"source_sha": source, "first_parent": history, "tags": tags, "releases": [release for page in pages for release in page]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "metadata"))
    parser.add_argument("--source-sha")
    parser.add_argument("--channel", choices=("stable", "dev"))
    parser.add_argument("--fixture", type=Path, help="read source_sha, first_parent, tags, and releases from JSON; no network access")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.fixture:
            payload = json.loads(args.fixture.read_text())
            if not isinstance(payload, dict):
                raise ReleaseError("release input must be a JSON object")
            if args.source_sha is not None and payload.get("source_sha") != require_sha(args.source_sha):
                raise ReleaseError("fixture source SHA does not match the requested source")
        else:
            if args.source_sha is None:
                raise ReleaseError("live requests require --source-sha")
            payload = live_payload(args.source_sha, args.repo_root)
        if args.command == "metadata":
            if args.channel is None:
                raise ReleaseError("metadata requires --channel")
            result = resolve_metadata(payload, args.channel)
        else:
            if args.channel is not None:
                raise ReleaseError("--channel is only supported by metadata")
            result = plan_release(payload)
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(encoded)
        if args.github_output:
            with args.github_output.open("a") as output:
                for key in ("version", "tag", "previous_tag", "source_sha", "needed"):
                    value = result[key]
                    output.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
        print(encoded, end="")
        return 0
    except (ReleaseError, OSError, json.JSONDecodeError) as error:
        print(f"Release planning failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
