from __future__ import annotations

import copy
import hashlib
import io
import json
import pathlib
import stat
import tempfile
import unittest
import warnings
import zipfile
from unittest.mock import patch

from scripts import publish_release, release_recovery
from scripts.tests.test_release_delivery import make_assets


RUN_ID = 101
SOURCE = "a" * 40
DELIVERY = "b" * 40
BASELINE = "c" * 40
PLATFORMS = ("linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-aarch64")


def qualification_fixture():
    run = {
        "id": RUN_ID, "path": ".github/workflows/release.yml", "event": "push",
        "head_branch": "main", "head_sha": SOURCE, "status": "completed", "conclusion": "failure",
        "repository": {"id": 123, "full_name": "y2-intel/harness"},
        "head_repository": {"id": 123, "full_name": "y2-intel/harness"},
    }
    names = ["plan", "release", "Build linux-x86_64", "Build linux-aarch64", "Build macos-x86_64"]
    for platform in PLATFORMS:
        names += [f"verify / Native checks (ReleaseSafe, {platform})", f"verify / Full suite ({platform})"]
        names += [f"verify / E2E (ReleaseSafe, {platform}, shard {shard}/4)" for shard in range(1, 5)]
    names += [f"build-macos-arm64 / {name}" for name in (
        "Build seed and plan shards", "Merge profiles and build candidate", "Aggregate release eligibility",
        "Package macOS arm64 CLI release", "Train training-01", "Train training-02",
        "Verify behavior-01", "Verify behavior-02", "Startup help", "Startup version", "Startup status",
        "Startup doctor", "Startup sessions", "Startup background", "Heavy approval-transcript",
        "Heavy approval-diff", "Heavy approval-payload", "Heavy approval-combined", "Heavy file-index-100k", "Heavy ui-activity",
    )]
    jobs = [{"name": name, "run_id": RUN_ID, "head_sha": SOURCE, "status": "completed",
             "conclusion": "failure" if name == "release" else "success"} for name in names]
    artifacts = [{
        "name": name, "id": index, "expired": False, "size_in_bytes": 10, "digest": "sha256:" + "d" * 64,
        "workflow_run": {"id": RUN_ID, "head_sha": SOURCE, "head_branch": "main", "repository_id": 123, "head_repository_id": 123},
    } for index, name in enumerate(["release-plan", *(f"y2-{p}" for p in PLATFORMS)], 1)]
    return run, jobs, artifacts


def zip_bytes(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def archive_metadata(name: str, data: bytes) -> dict:
    return {"name": name, "size_in_bytes": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}


def qualified_bundle(root: pathlib.Path, plan_changes: dict | None = None):
    make_assets(root)
    plan = {"schema_version": 1, "needed": True, "tag": "v0.0.8", "version": "0.0.8", "source_sha": SOURCE,
            "previous_tag": "v0.0.7", **(plan_changes or {})}
    run, jobs, artifacts = qualification_fixture()
    downloads = {}
    for artifact in artifacts:
        name = artifact["name"]
        entries = [("release-plan.json", json.dumps(plan).encode())] if name == "release-plan" else [
            (name + suffix, (root / (name + suffix)).read_bytes()) for suffix in (".tar.gz", ".tar.gz.sha256")
        ]
        data = zip_bytes(entries)
        artifact.update(archive_metadata(name, data))
        downloads[f"repos/y2-intel/harness/actions/artifacts/{artifact['id']}/zip"] = data
    def api(path, *, paginate=False):
        if path == f"actions/runs/{RUN_ID}":
            return run
        if path == f"actions/runs/{RUN_ID}/jobs?filter=latest&per_page=100":
            assert paginate
            return [{"jobs": jobs[:20]}, {"jobs": jobs[20:]}]
        if path == f"actions/runs/{RUN_ID}/artifacts?per_page=100":
            assert paginate
            return [{"artifacts": artifacts[:2]}, {"artifacts": artifacts[2:]}]
        raise AssertionError(path)
    def download(args):
        assert args[:2] == ["gh", "api"] and len(args) == 3
        return downloads[args[2]]
    return plan, api, download


class ReleaseQualificationTests(unittest.TestCase):
    def test_failed_publish_is_recoverable_only_after_all_other_jobs_pass(self):
        run, jobs, artifacts = qualification_fixture()
        self.assertEqual(set(release_recovery.validate_qualification(RUN_ID, run, jobs, artifacts)), release_recovery.ARTIFACTS)
        jobs[1]["conclusion"] = "success"
        run["conclusion"] = "success"
        self.assertEqual(len(release_recovery.validate_qualification(RUN_ID, run, jobs, artifacts)), 5)

    def test_rejects_wrong_workflow_nonmain_foreign_or_unfinished_run(self):
        cases = {
            "id": RUN_ID + 1, "path": ".github/workflows/ci.yml", "event": "pull_request",
            "head_branch": "feature", "head_sha": "short", "status": "in_progress", "conclusion": "cancelled",
            "repository": {"id": 123, "full_name": "foreign/harness"},
            "head_repository": {"id": 456, "full_name": "foreign/harness"},
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                run, jobs, artifacts = qualification_fixture()
                run[field] = value
                with self.assertRaisesRegex(ValueError, "canonical main Release"):
                    release_recovery.validate_qualification(RUN_ID, run, jobs, artifacts)
        for value in (0, -1, True, "101"):
            with self.subTest(run_id=value), self.assertRaisesRegex(ValueError, "positive release run ID"):
                release_recovery.validate_qualification(value, *qualification_fixture())

    def test_missing_duplicate_failed_skipped_and_cross_source_jobs_are_rejected(self):
        for kind in ("missing", "duplicate", "failed", "skipped", "unfinished", "cross-source", "cross-run", "no-training", "no-verification"):
            with self.subTest(kind=kind):
                run, jobs, artifacts = qualification_fixture()
                target = next(job for job in jobs if job["name"] == "verify / E2E (ReleaseSafe, linux-x86_64, shard 4/4)")
                if kind == "missing": jobs.remove(target)
                elif kind == "duplicate": jobs.append(copy.deepcopy(target))
                elif kind == "failed": target["conclusion"] = "failure"
                elif kind == "skipped": target["conclusion"] = "skipped"
                elif kind == "unfinished": target["status"] = "in_progress"
                elif kind == "cross-source": target["head_sha"] = DELIVERY
                elif kind == "cross-run": target["run_id"] = RUN_ID + 1
                else:
                    prefix = "build-macos-arm64 / Train " if kind == "no-training" else "build-macos-arm64 / Verify "
                    jobs = [job for job in jobs if not job["name"].startswith(prefix)]
                with self.assertRaisesRegex(ValueError, "qualification"):
                    release_recovery.validate_qualification(RUN_ID, run, jobs, artifacts)

    def test_artifacts_require_original_identity_digest_size_and_availability(self):
        cases = [("expired", True), ("digest", None), ("digest", "sha256:bad"), ("id", 0), ("id", True),
                 ("size_in_bytes", 0), ("size_in_bytes", 128 * 1024 * 1024 + 1),
                 ("workflow_run.id", RUN_ID + 1), ("workflow_run.head_sha", DELIVERY),
                 ("workflow_run.head_branch", "feature"), ("workflow_run.repository_id", 456), ("workflow_run.head_repository_id", 456)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                run, jobs, artifacts = qualification_fixture()
                target = artifacts[0]
                if field.startswith("workflow_run."):
                    target["workflow_run"][field.split(".")[1]] = value
                elif value is None:
                    target.pop(field)
                else:
                    target[field] = value
                with self.assertRaisesRegex(ValueError, "invalid qualified artifact"):
                    release_recovery.validate_qualification(RUN_ID, run, jobs, artifacts)
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                run, jobs, artifacts = qualification_fixture()
                artifacts = artifacts + [copy.deepcopy(artifacts[0])] if duplicate else artifacts[:-1]
                with self.assertRaisesRegex(ValueError, "artifact"):
                    release_recovery.validate_qualification(RUN_ID, run, jobs, artifacts)


class ReleaseArtifactTests(unittest.TestCase):
    def test_extracts_exact_bytes_and_never_overwrites_existing_input(self):
        data = zip_bytes([("release-plan.json", b'{"source_sha":"original"}')])
        artifact = archive_metadata("release-plan", data)
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            release_recovery.extract_artifact(artifact, data, root)
            self.assertEqual((root / "release-plan.json").read_bytes(), b'{"source_sha":"original"}')
            with self.assertRaises(FileExistsError):
                release_recovery.extract_artifact(artifact, data, root)

    def test_wrong_digest_or_size_is_rejected_before_extraction(self):
        data = zip_bytes([("release-plan.json", b"{}")])
        for change in ({"digest": "sha256:" + "0" * 64}, {"size_in_bytes": len(data) + 1}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                with self.assertRaisesRegex(ValueError, "immutable digest"):
                    release_recovery.extract_artifact({**archive_metadata("release-plan", data), **change}, data, root)
                self.assertEqual(list(root.iterdir()), [])

    def test_zip_rejects_traversal_absolute_nested_duplicate_and_symlink_members(self):
        symlink = zipfile.ZipInfo("release-plan.json")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [[("../release-plan.json", b"bad")], [("/release-plan.json", b"bad")],
                 [("nested/release-plan.json", b"bad")], [("release-plan.json", b"one"), ("release-plan.json", b"two")],
                 [(symlink, b"outside")], [("release-plan.json/", b"")]]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temp:
                data = zip_bytes(entries)
                root = pathlib.Path(temp)
                with self.assertRaisesRegex(ValueError, "artifact (paths|member)"):
                    release_recovery.extract_artifact(archive_metadata("release-plan", data), data, root)
                self.assertEqual(list(root.iterdir()), [])

    def test_downloaded_original_plan_must_match_qualified_source_and_version(self):
        for changes in ({"source_sha": DELIVERY}, {"schema_version": 2}, {"needed": False}, {"version": "0.0.9"}, {"tag": "v0.0.8-beta"}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                original = root / "original"
                original.mkdir()
                _, api, download = qualified_bundle(original, changes)
                with patch.object(release_recovery, "api", side_effect=api), patch.object(release_recovery.subprocess, "check_output", side_effect=download):
                    with self.assertRaisesRegex(ValueError, "original plan"):
                        release_recovery.download_qualified_inputs(RUN_ID, root / "recovered")


class ReleaseRecoveryPublicationTests(unittest.TestCase):
    def environment(self):
        return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "y2-intel/harness", "GITHUB_REF": "refs/heads/main",
                "GITHUB_SHA": DELIVERY, "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_WORKFLOW_REF": "y2-intel/harness/.github/workflows/recover-release.yml@refs/heads/main"}

    def test_recovery_requires_exact_canonical_workflow_and_normal_source_guard_remains(self):
        for change in ({"GITHUB_WORKFLOW_REF": "y2-intel/harness/.github/workflows/release.yml@refs/heads/main"},
                       {"GITHUB_WORKFLOW_REF": "y2-intel/harness/.github/workflows/recover-release.yml@refs/heads/feature"},
                       {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_REF": "refs/heads/feature"}):
            with self.subTest(change=change), patch.dict("os.environ", {**self.environment(), **change}), patch.object(publish_release, "run") as run:
                with self.assertRaisesRegex(ValueError, "canonical main"):
                    publish_release.validate_publish_source(SOURCE, "v0.0.8", qualified_run=RUN_ID)
                run.assert_not_called()
        with patch.dict("os.environ", self.environment()), patch.object(publish_release, "run") as run:
            with self.assertRaisesRegex(ValueError, "exact plan source"):
                publish_release.validate_publish_source(SOURCE, "v0.0.8")
            run.assert_not_called()

    def test_recovery_reuses_original_plan_and_bytes_and_refuses_missing_tag_or_draft(self):
        for scenario in ("happy", "changed-plan", "changed-bytes", "missing-tag", "missing-draft", "published"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                original = root / "original"
                original.mkdir()
                plan, recovery_api, download = qualified_bundle(original)
                with patch.object(release_recovery, "api", side_effect=recovery_api), patch.object(release_recovery.subprocess, "check_output", side_effect=download):
                    recovered = root / "recovered"
                    self.assertEqual(release_recovery.download_qualified_inputs(RUN_ID, recovered), plan)
                    assets = recovered / "assets"
                    for path in original.iterdir():
                        self.assertEqual((assets / path.name).read_bytes(), path.read_bytes())
                    if scenario == "changed-plan": plan = {**plan, "previous_tag": "v0.0.6"}
                    if scenario == "changed-bytes":
                        # Valid checksum semantics cannot substitute different qualified bytes.
                        name = "y2-linux-x86_64.tar.gz.sha256"
                        path = assets / name
                        path.write_text(path.read_text().rstrip("\n"))
                    digests = publish_release.verify_assets(assets)
                    draft = {"id": 17, "tag_name": "v0.0.8", "draft": scenario != "published", "prerelease": False, "assets": []}
                    baseline = {"id": 7, "tag_name": "v0.0.7", "draft": False, "prerelease": False}
                    mutations = []
                    uploaded = False
                    def optional(path):
                        if path == "git/ref/tags/v0.0.7": return {"object": {"type": "commit", "sha": BASELINE}}
                        if path == "git/ref/tags/v0.0.8":
                            return None if scenario == "missing-tag" else {"object": {"type": "commit", "sha": SOURCE}}
                        if path == "releases/latest": return baseline
                        raise AssertionError(path)
                    def api(path, *, payload=None):
                        self.assertIsNone(payload, "recovery must not create tags")
                        self.assertEqual(path, "releases/17")
                        return {**draft, "assets": [dict(name=name, state="uploaded", size=(assets / name).stat().st_size, digest="sha256:" + digests[name]) for name in sorted(digests)]} if uploaded else draft
                    def run(*args):
                        nonlocal uploaded
                        if args == ("git", "rev-parse", "HEAD"): return DELIVERY.encode()
                        if args[:2] == ("git", "fetch"): return b""
                        if args[:2] == ("git", "rev-list"): return f"{DELIVERY}\n{SOURCE}\n{BASELINE}\n".encode()
                        if args == ("gh", "api", "repos/y2-intel/harness/releases?per_page=100", "--paginate", "--slurp"):
                            return json.dumps([[baseline], [] if scenario == "missing-draft" else [draft]]).encode()
                        mutations.append(args)
                        self.assertEqual(scenario, "happy", "rejected recovery must not mutate remote state")
                        self.assertIn(args[:3], (("gh", "release", "upload"), ("gh", "release", "edit")))
                        if args[:3] == ("gh", "release", "upload"):
                            self.assertEqual(set(args[6:]), {str(assets / name) for name in publish_release.expected_assets()})
                            uploaded = True
                        else:
                            self.assertTrue(uploaded)
                        return b""
                    with patch.dict("os.environ", self.environment()), patch.object(publish_release, "run", side_effect=run), patch.object(publish_release, "optional_api", side_effect=optional), patch.object(publish_release, "api", side_effect=api):
                        if scenario == "happy":
                            publish_release.publish(plan, assets, root / "notes.md", qualified_run=RUN_ID)
                            self.assertEqual([args[:3] for args in mutations], [("gh", "release", "upload"), ("gh", "release", "edit")])
                        else:
                            with self.assertRaisesRegex(ValueError, "exact qualified|existing immutable|existing release draft|unpublished release draft"):
                                publish_release.publish(plan, assets, root / "notes.md", qualified_run=RUN_ID)
                            self.assertEqual(mutations, [])


if __name__ == "__main__":
    unittest.main()
