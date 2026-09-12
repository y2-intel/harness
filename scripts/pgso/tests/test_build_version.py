from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@unittest.skipUnless(shutil.which("zig") and shutil.which("git"), "requires Zig and Git")
class BuildVersionTests(unittest.TestCase):
    def run_command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=self.root, capture_output=True, text=True, check=True, timeout=90,
        )

    def built_version(self, override: str | None = None) -> str:
        args = ["zig", "build"]
        if override is not None:
            args.append(f"-Dapp-version={override}")
        self.run_command(*args)
        return self.run_command(str(self.root / "zig-out/bin/y2")).stdout.strip()

    def test_build_uses_first_parent_tags_and_explicit_override_without_source_edits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="y2-build-version-") as temporary:
            self.root = pathlib.Path(temporary)
            shutil.copy2(REPO_ROOT / "build.zig", self.root / "build.zig")
            (self.root / "src").mkdir()
            (self.root / "src/main.zig").write_text('''const std = @import("std");
const options = @import("build_options");
pub fn main(init: std.process.Init) !void {
    var buffer: [128]u8 = undefined;
    var output = std.Io.File.stdout().writer(init.io, &buffer);
    try output.interface.writeAll(options.app_version);
    try output.interface.flush();
}
''')
            self.run_command("git", "init", "-b", "main")
            self.run_command("git", "config", "user.name", "Version fixture")
            self.run_command("git", "config", "user.email", "fixture@example.invalid")
            self.run_command("git", "add", "build.zig", "src/main.zig")
            self.run_command("git", "commit", "-m", "Initial fixture")
            self.run_command("git", "tag", "v0.0.7")
            self.run_command("git", "switch", "-c", "upstream")
            self.run_command("git", "commit", "--allow-empty", "-m", "Upstream fixture")
            self.run_command("git", "tag", "v99.0.0")
            self.run_command("git", "switch", "main")
            self.run_command("git", "merge", "--no-ff", "upstream", "-m", "Merge fixture")
            self.run_command("git", "tag", "v0.1.0-dev")
            self.run_command("git", "tag", "v00.1.0")
            self.assertEqual("0.0.7", self.built_version())
            self.assertEqual("0.0.8", self.built_version("0.0.8"))
            self.assertEqual(
                "",
                self.run_command("git", "status", "--porcelain", "--untracked-files=no").stdout,
            )
            invalid = subprocess.run(
                ["zig", "build", "-Dapp-version=0.0.8-dev"],
                cwd=self.root, capture_output=True, text=True, timeout=90,
            )
            self.assertNotEqual(0, invalid.returncode)
            self.assertIn("-Dapp-version must be strict", invalid.stderr)
            shutil.rmtree(self.root / ".git")
            self.assertEqual("0.0.0", self.built_version())
