from __future__ import annotations

import hashlib
import io
import json
import pathlib
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish_release
from scripts.publish_release import expected_assets, validate_remote_assets, verify_assets
from scripts.release_notes import render_notes


def make_assets(root: pathlib.Path, extra_member: str | None = None) -> None:
    for name in sorted(expected_assets()):
        if name.endswith(".sha256"):
            continue
        path = root / name
        with tarfile.open(path, "w:gz") as archive:
            for member in ["y2", "LICENSE", "THIRD_PARTY_NOTICES.md"] + ([extra_member] if extra_member else []):
                data = b"release fixture\n"
                info = tarfile.TarInfo(member)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        (root / (name + ".sha256")).write_text(f"{checksum}  {name}\n")


class ReleaseDeliveryTests(unittest.TestCase):
    def test_release_discovery_includes_drafts_beyond_first_page(self):
        draft = {"id": 17, "tag_name": "v0.0.8", "draft": True, "assets": []}
        pages = [[{"id": 7, "tag_name": "v0.0.7", "draft": False}], [draft]]
        with patch.object(publish_release, "run", return_value=json.dumps(pages).encode()) as run:
            self.assertEqual(publish_release.find_release("v0.0.8"), draft)
            run.assert_called_once_with("gh", "api", "repos/y2-intel/harness/releases?per_page=100", "--paginate", "--slurp")

    def test_release_discovery_rejects_duplicate_drafts_for_same_tag(self):
        pages = [[{"id": 17, "tag_name": "v0.0.8"}], [{"id": 18, "tag_name": "v0.0.8"}]]
        with patch.object(publish_release, "run", return_value=json.dumps(pages).encode()):
            with self.assertRaisesRegex(ValueError, "multiple releases"):
                publish_release.find_release("v0.0.8")

    def test_numeric_refresh_rejects_missing_or_changed_release_identity(self):
        for release_id in (None, 0, -1, True, "17"):
            with self.subTest(release_id=release_id), patch.object(publish_release, "api") as api:
                with self.assertRaisesRegex(ValueError, "numeric ID"):
                    publish_release.refresh_release({"id": release_id}, "v0.0.8")
                api.assert_not_called()
        for current in ({"id": 18, "tag_name": "v0.0.8"}, {"id": 17, "tag_name": "v0.0.9"}):
            with self.subTest(current=current), patch.object(publish_release, "api", return_value=current) as api:
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    publish_release.refresh_release({"id": 17}, "v0.0.8")
                api.assert_called_once_with("releases/17")

    def test_publication_rejects_wrong_branch_or_event_source_before_any_mutation(self):
        source = "a" * 40
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "y2-intel/harness",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": source,
        }
        for field, wrong in (("GITHUB_REF", "refs/heads/feature"), ("GITHUB_SHA", "b" * 40), ("GITHUB_REPOSITORY", "another/repository")):
            with self.subTest(field=field), patch.dict("os.environ", {**environment, field: wrong}), patch.object(publish_release, "run") as run, patch.object(publish_release, "api") as api, patch.object(publish_release, "optional_api") as optional:
                with self.assertRaisesRegex(ValueError, "canonical main CI"):
                    publish_release.publish({"tag": "v0.0.8", "source_sha": source}, pathlib.Path("unused"), pathlib.Path("unused"))
                run.assert_not_called()
                api.assert_not_called()
                optional.assert_not_called()

    def test_publication_rejects_stale_source_before_tag_or_draft_creation(self):
        source, newer, baseline = "a" * 40, "b" * 40, "c" * 40
        releases = [[{"tag_name": "v0.1.0", "draft": False, "prerelease": False}]]
        replies = [(source + "\n").encode(), b"", (newer + "\n" + source + "\n" + baseline + "\n").encode(), json.dumps(releases).encode()]
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "y2-intel/harness", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": source}
        with patch.dict("os.environ", environment), patch.object(publish_release, "run", side_effect=replies) as run, patch.object(publish_release, "api") as api, patch.object(publish_release, "optional_api", return_value={"object": {"type": "commit", "sha": newer}}) as optional, patch.object(publish_release, "verify_assets") as verify:
            with self.assertRaisesRegex(ValueError, "out-of-order"):
                publish_release.publish({"tag": "v0.0.8", "source_sha": source}, pathlib.Path("unused"), pathlib.Path("unused"))
            api.assert_not_called()
            verify.assert_not_called()
            optional.assert_called_once_with("git/ref/tags/v0.1.0")
            self.assertFalse(any(call.args[:3] in (("gh", "release", "create"), ("gh", "release", "edit")) for call in run.call_args_list))

    def test_publication_rejects_source_outside_main_lineage(self):
        source, main = "a" * 40, "b" * 40
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "y2-intel/harness", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": source}
        with patch.dict("os.environ", environment), patch.object(publish_release, "run", side_effect=[(source + "\n").encode(), b"", (main + "\n").encode()]), patch.object(publish_release, "api") as api:
            with self.assertRaisesRegex(ValueError, "origin/main"):
                publish_release.validate_publish_source(source, "v0.0.8")
            api.assert_not_called()

    def test_publication_accepts_pinned_ancestor_and_ignores_side_branch_release(self):
        source, main, baseline, upstream = "a" * 40, "b" * 40, "c" * 40, "d" * 40
        releases = [[{"tag_name": name, "draft": False, "prerelease": False} for name in ("v0.4.5", "v0.0.7")]]
        targets = {"git/ref/tags/v0.4.5": upstream, "git/ref/tags/v0.0.7": baseline}
        replies = [(source + "\n").encode(), b"", (main + "\n" + source + "\n" + baseline + "\n").encode(), json.dumps(releases).encode()]
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "y2-intel/harness", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": source}
        with patch.dict("os.environ", environment), patch.object(publish_release, "run", side_effect=replies), patch.object(publish_release, "optional_api", side_effect=lambda path: {"object": {"type": "commit", "sha": targets[path]}}):
            publish_release.validate_publish_source(source, "v0.0.8")

    def test_complete_four_platform_release_matches_every_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root)
            self.assertEqual(set(verify_assets(root)), expected_assets())

    def test_missing_platform_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root)
            (root / "y2-macos-aarch64.tar.gz").unlink()
            with self.assertRaisesRegex(ValueError, "exactly four"):
                verify_assets(root)

    def test_corrupt_archive_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root)
            path = root / "y2-linux-x86_64.tar.gz.sha256"
            path.write_text("0" * 64 + "  y2-linux-x86_64.tar.gz\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_assets(root)

    def test_extra_archive_paths_cannot_reach_clients(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root, "../outside")
            with self.assertRaisesRegex(ValueError, "unexpected archive contents"):
                verify_assets(root)

    def test_retry_refuses_different_already_uploaded_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root)
            digests = verify_assets(root)
            name = "y2-macos-aarch64.tar.gz"
            asset = dict(name=name, state="uploaded", size=(root / name).stat().st_size, digest="sha256:" + "0" * 64)
            with self.assertRaisesRegex(ValueError, "refusing to replace"):
                validate_remote_assets({"assets": [asset]}, digests, root)

    def test_retry_reuses_verified_assets_and_identifies_missing_platforms(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root)
            digests = verify_assets(root)
            names = sorted(expected_assets())[:3]
            assets = [dict(name=n, state="uploaded", size=(root / n).stat().st_size, digest="sha256:" + digests[n]) for n in names]
            with patch("scripts.publish_release.run", side_effect=AssertionError("verified hashes need no download")):
                existing = validate_remote_assets({"assets": assets}, digests, root)
            self.assertEqual(existing, set(names))
            self.assertEqual(len(expected_assets() - existing), 5)

    def test_interrupted_draft_upload_is_deleted_then_original_assets_publish(self):
        source = "a" * 40
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root)
            digests = verify_assets(root)
            starter = {"id": 123, "name": "y2-linux-x86_64.tar.gz", "size": 0, "state": "starter"}
            draft = {"id": 17, "tag_name": "v0.0.8", "draft": True, "prerelease": False, "assets": [starter]}
            complete = {**draft, "assets": [dict(name=name, state="uploaded", size=(root / name).stat().st_size, digest="sha256:" + digests[name]) for name in sorted(expected_assets())]}
            def optional(path):
                if path == "git/ref/tags/v0.0.8": return {"object": {"type": "commit", "sha": source}}
                if path == "releases/latest": return {"tag_name": "v0.0.7"}
                raise AssertionError(path)
            with patch.object(publish_release, "validate_publish_source"), patch.object(publish_release, "optional_api", side_effect=optional), patch.object(publish_release, "find_release", return_value=draft), patch.object(publish_release, "api", side_effect=[draft, complete]) as api, patch.object(publish_release, "run", return_value=b"") as run:
                publish_release.publish({"tag": "v0.0.8", "source_sha": source}, root, root / "notes.md")
            self.assertEqual([call.args for call in api.call_args_list], [("releases/17",), ("releases/17",)])
            calls = [call.args for call in run.call_args_list]
            self.assertEqual(calls[0], ("gh", "api", "repos/y2-intel/harness/releases/assets/123", "--method", "DELETE"))
            self.assertEqual(calls[1][:5], ("gh", "release", "upload", "v0.0.8", "--repo"))
            self.assertEqual(set(calls[1][6:]), {str(root / name) for name in expected_assets()})
            self.assertEqual(calls[2][:3], ("gh", "release", "edit"))

    def test_new_and_existing_drafts_publish_without_release_by_tag_endpoint(self):
        source = "a" * 40
        for exists in (False, True):
            with self.subTest(existing_draft=exists), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                make_assets(root)
                digests = verify_assets(root)
                draft = {"id": 17, "tag_name": "v0.0.8", "draft": True, "prerelease": False, "assets": []}
                complete = {**draft, "assets": [dict(name=name, state="uploaded", size=(root / name).stat().st_size, digest="sha256:" + digests[name]) for name in sorted(expected_assets())]}
                created = exists
                uploaded = False
                def optional(path):
                    if path == "git/ref/tags/v0.0.8": return {"object": {"type": "commit", "sha": source}}
                    if path == "releases/latest": return {"tag_name": "v0.0.7"}
                    raise AssertionError(f"unexpected lookup: {path}")
                def api(path):
                    self.assertEqual(path, "releases/17")
                    return complete if uploaded else draft
                def run(*args):
                    nonlocal created, uploaded
                    if args == ("gh", "api", "repos/y2-intel/harness/releases?per_page=100", "--paginate", "--slurp"):
                        return json.dumps([[{"id": 7, "tag_name": "v0.0.7"}], [draft] if created else []]).encode()
                    if args[:3] == ("gh", "release", "create"):
                        self.assertFalse(created, "existing draft must be reused")
                        self.assertIn("--verify-tag", args)
                        self.assertIn("--draft", args)
                        created = True
                    elif args[:3] == ("gh", "release", "upload"):
                        self.assertTrue(created)
                        self.assertEqual(set(args[6:]), {str(root / name) for name in expected_assets()})
                        uploaded = True
                    elif args[:3] == ("gh", "release", "edit"):
                        self.assertTrue(uploaded, "publish only after complete asset validation")
                        self.assertIn("--draft=false", args)
                    else:
                        raise AssertionError(args)
                    return b""
                with patch.object(publish_release, "validate_publish_source"), patch.object(publish_release, "optional_api", side_effect=optional), patch.object(publish_release, "api", side_effect=api) as api_mock, patch.object(publish_release, "run", side_effect=run) as run_mock:
                    publish_release.publish({"tag": "v0.0.8", "source_sha": source}, root, root / "notes.md")
                self.assertEqual(api_mock.call_count, 2)
                calls = [call.args[:3] for call in run_mock.call_args_list]
                self.assertEqual(calls.count(("gh", "release", "create")), 0 if exists else 1)
                self.assertEqual(calls.count(("gh", "release", "edit")), 1)

    def test_existing_tag_with_different_source_cannot_reach_draft_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            make_assets(root)
            with patch.object(publish_release, "validate_publish_source"), patch.object(publish_release, "optional_api", return_value={"object": {"type": "commit", "sha": "b" * 40}}), patch.object(publish_release, "run") as run, patch.object(publish_release, "api") as api:
                with self.assertRaisesRegex(ValueError, "never retag"):
                    publish_release.publish({"tag": "v0.0.8", "source_sha": "a" * 40}, root, root / "notes.md")
                run.assert_not_called()
                api.assert_not_called()

    def test_recovery_never_deletes_published_uploaded_nonempty_or_unknown_assets(self):
        starter = {"id": 123, "name": "y2-linux-x86_64.tar.gz", "size": 0, "state": "starter"}
        cases = [
            {"draft": False, "assets": [starter]},
            {"draft": True, "assets": [{**starter, "state": "uploaded"}]},
            {"draft": True, "assets": [{**starter, "size": 1}]},
            {"draft": True, "assets": [{**starter, "name": "unrelated.txt"}]},
        ]
        for case in cases:
            with self.subTest(case=case), patch.object(publish_release, "run") as run:
                self.assertEqual(publish_release.remove_empty_draft_uploads(case), case)
                run.assert_not_called()

    def test_notes_preserve_product_copy_and_group_independent_fragments(self):
        notes = render_notes("v0.0.8", ["### Bug Fixes\n\n- **Recovery:** Retain completed answers.\n", "### New Features\n\n- **MCP:** Connect approved project servers.\n"])
        self.assertLess(notes.index("New Features"), notes.index("Bug Fixes"))
        self.assertIn("- **Recovery:** Retain completed answers.", notes)

    def test_notes_do_not_invent_user_changes_for_maintenance_release(self):
        self.assertEqual(render_notes("v0.0.9", []), "y2 v0.0.9\n")

    def test_notes_reject_trackers_and_invalid_structure(self):
        for fragment in ["### Contributors\n- **Thanks:** Someone.\n", "### Bug Fixes\n- **Fix:** PR #123.\n", "### Bug Fixes\nUnreviewed paragraph\n"]:
            with self.subTest(fragment=fragment), self.assertRaises(ValueError):
                render_notes("v0.0.8", [fragment])


if __name__ == "__main__":
    unittest.main()
