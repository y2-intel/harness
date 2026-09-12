import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "release.py"
SPEC = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SOURCE = "a" * 40
PARENT = "b" * 40
BASE = "c" * 40
UPSTREAM = "d" * 40


def published(tag="v0.0.7", *, complete=True):
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": name, "size": 123, "state": "uploaded"}
            for name in sorted(release.EXPECTED_ASSETS)
        ] if complete else [],
    }


def fixture():
    return {
        "source_sha": SOURCE,
        "first_parent": [
            {"sha": SOURCE, "message": "Merge the reviewed change"},
            {"sha": PARENT, "message": "Fix a problem"},
            {"sha": BASE, "message": "Prior release #major"},
        ],
        "tags": {"v0.0.7": BASE, "v0.4.5": UPSTREAM},
        "releases": [published()],
    }


class ReleasePlanningTests(unittest.TestCase):
    def test_versions_match_updater_component_and_length_bounds(self):
        maximum = (1 << 32) - 1
        self.assertEqual(release.parse_version(f"v{maximum}.{maximum}.{maximum}"), (maximum, maximum, maximum))
        for tag in (f"v{maximum + 1}.0.0", f"v0.{maximum + 1}.0", f"v0.0.{maximum + 1}", "v" + "9" * 10000 + ".0.0", "v00.1.0"):
            with self.subTest(tag=tag[:40]):
                self.assertIsNone(release.parse_version(tag))

    def test_bump_overflow_fails_without_wrapping_or_invalid_tags(self):
        maximum = (1 << 32) - 1
        for version, messages in (((1, 2, maximum), []), ((1, maximum, 0), ["#minor"]), ((maximum, 0, 0), ["#major"])):
            with self.subTest(version=version), self.assertRaisesRegex(release.ReleaseError, "SemVer limits"):
                release.bump_version(version, messages)
        self.assertEqual(release.bump_version((1, 2, maximum), ["#minor"]), ((1, 3, 0), "minor"))

    def test_default_patch_ignores_inherited_tags_and_released_markers(self):
        result = release.plan_release(fixture())
        self.assertEqual((result["version"], result["previous_tag"], result["source_sha"]), ("0.0.8", "v0.0.7", SOURCE))
        self.assertTrue(result["needed"])

    def test_first_parent_range_includes_earlier_commits_and_merge_body(self):
        data = fixture()
        data["first_parent"][1]["message"] = "Add feature\n\n#MINOR"
        self.assertEqual(release.plan_release(data)["tag"], "v0.1.0")
        data["first_parent"][0]["message"] = "Merge branch\n\nThis changes the API (#MaJoR)."
        self.assertEqual(release.plan_release(data)["tag"], "v1.0.0")

    def test_marker_requires_standalone_word(self):
        for message in ("#majority", "#minor_fix", "#major-change", "a#major", "##major"):
            with self.subTest(message=message):
                self.assertEqual(release.bump_version((1, 2, 3), [message]), ((1, 2, 4), "patch"))

    def test_side_branch_published_release_is_not_a_y2_baseline(self):
        data = fixture()
        data["releases"].append(published("v0.4.5"))
        self.assertEqual(release.plan_release(data)["tag"], "v0.0.8")

    def test_highest_published_semver_is_used_not_api_order(self):
        data = fixture()
        data["tags"]["v0.1.2"] = PARENT
        data["releases"].insert(0, published("v0.1.2"))
        self.assertEqual(release.plan_release(data)["tag"], "v0.1.3")

    def test_tag_only_retry_reuses_reserved_version(self):
        data = fixture()
        data["tags"]["v0.0.8"] = SOURCE
        result = release.plan_release(data)
        self.assertEqual(result["tag"], "v0.0.8")
        self.assertEqual(result["bump"], "reuse")
        self.assertTrue(result["needed"])
        self.assertTrue(result["tag_exists"])

    def test_incomplete_published_release_is_resumed_without_bumping(self):
        data = fixture()
        data["tags"]["v0.0.8"] = SOURCE
        data["releases"].append(published("v0.0.8", complete=False))
        result = release.plan_release(data)
        self.assertEqual(result["tag"], "v0.0.8")
        self.assertEqual(result["previous_tag"], "v0.0.7")
        self.assertTrue(result["needed"])

    def test_draft_release_resumes_at_reserved_tag(self):
        data = fixture()
        data["tags"]["v0.0.8"] = SOURCE
        draft = published("v0.0.8")
        draft["draft"] = True
        data["releases"].append(draft)
        self.assertTrue(release.plan_release(data)["needed"])

    def test_complete_published_exact_source_is_idempotent(self):
        data = fixture()
        data["tags"]["v0.0.8"] = SOURCE
        data["releases"].append(published("v0.0.8"))
        self.assertFalse(release.plan_release(data)["needed"])

    def test_asset_completeness_rejects_missing_duplicate_empty_or_unuploaded(self):
        for kind in ("missing", "duplicate", "empty", "unuploaded"):
            data = published()
            if kind == "missing": data["assets"].pop()
            if kind == "duplicate": data["assets"].append(data["assets"][0].copy())
            if kind == "empty": data["assets"][0]["size"] = 0
            if kind == "unuploaded": data["assets"][0]["state"] = "starter"
            with self.subTest(kind=kind):
                self.assertFalse(release.complete_release(data))

    def test_wrong_sha_tag_collision_fails_closed(self):
        data = fixture()
        data["tags"]["v0.0.8"] = UPSTREAM
        with self.assertRaisesRegex(release.ReleaseError, "refusing to retag"):
            release.plan_release(data)

    def test_ambiguous_reservations_fail_closed(self):
        data = fixture()
        data["tags"].update({"v0.0.8": SOURCE, "v0.0.9": SOURCE})
        with self.assertRaisesRegex(release.ReleaseError, "multiple SemVer"):
            release.plan_release(data)

    def test_no_canonical_baseline_fails_instead_of_inventing_version(self):
        data = fixture()
        data["releases"] = []
        with self.assertRaisesRegex(release.ReleaseError, "explicit release baseline"):
            release.plan_release(data)

    def test_missing_published_tag_fails_closed(self):
        data = fixture()
        del data["tags"]["v0.0.7"]
        with self.assertRaisesRegex(release.ReleaseError, "missing its immutable tag"):
            release.plan_release(data)

    def test_nonstable_releases_do_not_set_baseline(self):
        for flag in ("draft", "prerelease"):
            data = fixture()
            data["tags"]["v9.0.0"] = PARENT
            ignored = published("v9.0.0")
            ignored[flag] = True
            data["releases"].append(ignored)
            with self.subTest(flag=flag):
                self.assertEqual(release.plan_release(data)["tag"], "v0.0.8")

    def test_stable_metadata_requires_exact_complete_release(self):
        data = fixture()
        with self.assertRaisesRegex(release.ReleaseError, "exact source SHA"):
            release.resolve_metadata(data, "stable")
        data["tags"]["v0.0.8"] = SOURCE
        data["releases"].append(published("v0.0.8", complete=False))
        with self.assertRaises(release.ReleaseError):
            release.resolve_metadata(data, "stable")
        data["releases"][-1] = published("v0.0.8")
        self.assertEqual(release.resolve_metadata(data, "stable")["version"], "0.0.8")

    def test_dev_metadata_uses_published_ancestor_without_allocating(self):
        result = release.resolve_metadata(fixture(), "dev")
        self.assertEqual((result["version"], result["source_sha"]), ("0.0.7", SOURCE))

    def test_fixture_cli_writes_plan_and_github_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "input.json"
            path.write_text(json.dumps(fixture()))
            output = root / "plan.json"
            github_output = root / "github-output"
            result = subprocess.run([sys.executable, str(SCRIPT), "plan", "--fixture", str(path), "--source-sha", SOURCE, "--output", str(output), "--github-output", str(github_output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text())["tag"], "v0.0.8")
            self.assertIn("needed=true\n", github_output.read_text())
            self.assertIn(f"source_sha={SOURCE}\n", github_output.read_text())

    def test_live_boundary_pins_sha_peels_tags_and_never_uses_shell(self):
        history = "".join(f'{item["sha"]}\0{item["message"]}\0\n' for item in fixture()["first_parent"])
        replies = [SOURCE + "\n", "git@github.com:y2-intel/harness.git\n", f'{UPSTREAM}\trefs/tags/v0.0.7\n{BASE}\trefs/tags/v0.0.7^{{}}\n', history, json.dumps([[published()]])]
        def fake_run(argv, **kwargs):
            self.assertIsInstance(argv, list)
            self.assertNotIn("shell", kwargs)
            return subprocess.CompletedProcess(argv, 0, replies.pop(0), "")
        with mock.patch.object(release.subprocess, "run", side_effect=fake_run) as called:
            payload = release.live_payload(SOURCE, Path.cwd())
        self.assertEqual(payload["tags"]["v0.0.7"], BASE)
        self.assertEqual(release.plan_release(payload)["version"], "0.0.8")
        self.assertIn(SOURCE, called.call_args_list[3].args[0])

    def test_invalid_source_is_rejected_before_any_process(self):
        with mock.patch.object(release.subprocess, "run") as called:
            with self.assertRaises(release.ReleaseError):
                release.live_payload("main; touch /tmp/unsafe", Path.cwd())
            called.assert_not_called()


if __name__ == "__main__":
    unittest.main()
