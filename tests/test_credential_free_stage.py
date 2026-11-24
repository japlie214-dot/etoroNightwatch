"""Standalone unittest: synthetic staging only; no pytest/conftest or runtime data.

Run with Python -I -B and --scratch pointing at the session scratchpad.
"""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRATCH = None
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("credential_free_stage", ROOT / "tools/credential_free_stage.py")
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


class StageTests(unittest.TestCase):
    def setUp(self):
        if SCRATCH is None:
            self.skipTest("run standalone with an explicit session --scratch directory")
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.destination = self.base / "clean"
        self.name = "android-probe/app/src/main/java/Example.kt"
        source = self.root / self.name
        source.parent.mkdir(parents=True)
        source.write_bytes(b"// synthetic public source\n")
        self.approved = [{"path": self.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]

    def test_only_exact_approved_source_is_read_and_copied(self):
        original = Path.read_bytes
        reads = []
        def read(path):
            reads.append(path)
            return original(path)
        with patch.object(Path, "read_bytes", read):
            self.assertEqual(1, staging.stage(self.root, self.destination, self.approved))
        self.assertEqual([self.root / self.name], reads)
        self.assertEqual([self.name], [p.relative_to(self.destination).as_posix()
                                      for p in self.destination.rglob("*") if p.is_file()])
        self.assertEqual(original(self.root / self.name), original(self.destination / self.name))

    def test_private_generated_and_escape_paths_rejected_before_any_content_read(self):
        names = ["local.properties", "gradle.properties", "android-probe/local.properties",
                 "android-probe/app/google-services.json", "android-probe/app/build/generated/Foo.kt",
                 "android-probe/app/src/main/assets/config.xml",
                 "android-probe/app/src/main/python/secrets.py",
                 "android-probe/app/src/main/python/__pycache__/a.py",
                 "android-probe/app/src/main/java/NUL.kt",
                 "android-probe/app/src/main/java/../Foo.kt",
                 "/absolute.kt", "C:/outside.kt", "../outside.kt", "a\\outside.kt",
                 "android-probe/app/src/main/java/X.kt:stream", "app/src/main/python/.env",
                 "android-probe/app/src/main/java/X.kt."]
        for name in names:
            with self.subTest(name=name), patch.object(Path, "read_bytes", side_effect=AssertionError("content read")):
                with self.assertRaises(ValueError):
                    staging.stage(self.root, self.destination,
                                  self.approved + [{"path": name, "sha256": "0" * 64}])
            self.assertFalse(self.destination.exists())

    def test_required_python_package_sql_and_initializers_can_be_approved(self):
        names = ["app/src/main/python/nightwatch/__init__.py",
                 "app/src/main/python/nightwatch/db/schema.sql",
                 "app/src/main/python/nightwatch/db/migrations/002_position_amount.sql"]
        approvals = []
        for name in names:
            source = self.root / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"-- synthetic public package input\n")
            approvals.append({"path": name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        self.assertEqual(3, staging.stage(self.root, self.destination, approvals))
        for name in names:
            self.assertEqual((self.root / name).read_bytes(), (self.destination / name).read_bytes())
        for name in ("app/src/main/python/other.sql", "app/src/main/python/nightwatch/db/private.sql",
                     "app/src/main/python/nightwatch/db/migrations/credentials.sql"):
            with self.assertRaises(ValueError):
                staging.source_path(name)

    def test_changed_bytes_are_not_staged(self):
        self.approved[0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "differs"):
            staging.stage(self.root, self.destination, self.approved)
        self.assertFalse(self.destination.exists())

    def test_duplicate_or_invalid_approval_is_rejected(self):
        for approvals in ([], self.approved * 2, [{"path": self.name}],
                          [{"path": self.name, "sha256": "not-a-hash"}],
                          [dict(self.approved[0], extra=True)]):
            with self.subTest(approvals=approvals), self.assertRaises(ValueError):
                staging.stage(self.root, self.destination, approvals)
        self.assertFalse(self.destination.exists())

    def test_workspace_or_existing_destination_is_rejected(self):
        for dest in (self.root / "clean", self.root, self.base):
            with self.subTest(dest=dest), self.assertRaises(ValueError):
                staging.stage(self.root, dest, self.approved)

    def test_hardlinked_file_is_rejected_without_reading(self):
        source = self.root / self.name
        (self.base / "alias.kt").hardlink_to(source)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("content read")):
            with self.assertRaisesRegex(ValueError, "non-hardlinked"):
                staging.stage(self.root, self.destination, self.approved)

    def test_symlink_is_rejected_without_reading(self):
        with patch.object(Path, "is_symlink", return_value=True), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("content read")):
            with self.assertRaisesRegex(ValueError, "linked"):
                staging.stage(self.root, self.destination, self.approved)

    def test_windows_junction_is_rejected_without_reading(self):
        class Reparse:
            st_file_attributes = staging.stat.FILE_ATTRIBUTE_REPARSE_POINT
        with patch.object(Path, "lstat", return_value=Reparse()), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("content read")):
            with self.assertRaisesRegex(ValueError, "reparse"):
                staging.stage(self.root, self.destination, self.approved)


class InventoryTests(unittest.TestCase):
    def tracked(self):
        return sorted(staging.BUILD_FILES) + [
            "android-probe/app/src/main/AndroidManifest.xml",
            "android-probe/app/src/main/java/Example.kt",
            "android-probe/app/src/main/res/values/strings.xml",
            "android-probe/app/src/main/python/probe/checks.py",
            "app/src/main/python/nightwatch/__init__.py",
            "app/src/main/python/nightwatch/db/schema.sql",
            "app/src/main/python/nightwatch/db/migrations/002_position_amount.sql",
            "android-probe/gradle.properties",
            "android-probe/app/google-services.json",
            "android-probe/app/src/test/java/ExampleTest.kt",
        ]

    def test_inventory_does_not_read_hash_stage_or_approve_any_contents(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("content read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("content read")), \
                patch.object(Path, "open", side_effect=AssertionError("content open")), \
                patch.object(staging.hashlib, "sha256", side_effect=AssertionError("hash")), \
                patch.object(staging, "stage", side_effect=AssertionError("staging")):
            result = staging.inventory(self.tracked())
        self.assertFalse(result["build_ready"])
        self.assertEqual([], result["missing_source_groups"])
        self.assertEqual([], result["missing_required_paths"])
        self.assertEqual([], result["staging_rule_rejections"])
        self.assertIn("none granted", result["approval"])
        self.assertIn("android-probe/gradle.properties", result["excluded_build_state_paths"])
        self.assertNotIn("android-probe/app/google-services.json",
                         sum(result["source_groups"].values(), []))
        self.assertIn("compatible_external_signing_gate", result)
        self.assertIn("runtime_provisioning_gate", result)
        tools = result["unsigned_compile_prerequisites"]["tool_versions_required_not_probed"]
        self.assertEqual("17", tools["JDK"])
        self.assertEqual("2.0.21", tools["Kotlin plugin"])

    def test_missing_paths_groups_and_staging_rejections_stay_explicit(self):
        result = staging.inventory(["android-probe/app/src/main/python/secrets.py"])
        self.assertIn("build_definitions", result["missing_source_groups"])
        self.assertIn("nightwatch_sql_migrations", result["missing_source_groups"])
        self.assertIn("android-probe/app/build.gradle.kts", result["missing_required_paths"])
        self.assertEqual("android-probe/app/src/main/python/secrets.py",
                         result["staging_rule_rejections"][0]["path"])
        self.assertFalse(result["build_ready"])

    def test_cli_inventory_calls_only_git_path_metadata(self):
        with patch("sys.argv", ["stage", "--root", "synthetic-root", "--inventory"]), \
                patch.object(staging.subprocess, "run") as run, \
                patch.object(Path, "read_text", side_effect=AssertionError("content read")), \
                patch.object(staging, "stage", side_effect=AssertionError("staging")), \
                patch("builtins.print") as output:
            run.return_value.stdout = "\0".join(self.tracked()).encode() + b"\0"
            staging.main()
        run.assert_called_once_with(
            ["git", "-C", "synthetic-root", "ls-files", "-z", "--", "android-probe", "app/src/main/python"],
            check=True, capture_output=True,
        )
        result = staging.json.loads(output.call_args.args[0])
        self.assertEqual("metadata-only-dry-run", result["mode"])
        self.assertFalse(result["build_ready"])

    def test_inventory_cannot_accept_approval_or_destination(self):
        for option in ("--approvals", "--destination"):
            with self.subTest(option=option), \
                    patch("sys.argv", ["stage", "--root", "synthetic-root", "--inventory", option, "not-opened"]), \
                    patch.object(Path, "read_text", side_effect=AssertionError("content read")), \
                    patch.object(staging.subprocess, "run", side_effect=AssertionError("metadata read")), \
                    patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                staging.main()
            self.assertEqual(2, error.exception.code)


class SourceWiringTests(unittest.TestCase):
    def assert_credential_free_build(self, build):
        self.assertNotIn("local.properties", build)
        self.assertNotIn("FileInputStream", build)
        for name in ("PHONE_PUSH_SECRET", "COMMAND_HMAC_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            self.assertNotIn(name, build)

    def test_no_build_time_secret_source_or_field(self):
        build = (ROOT / "android-probe/app/build.gradle.kts").read_text(encoding="utf-8")
        self.assert_credential_free_build(build)

    def test_build_guard_catches_synthetic_previous_coupling(self):
        for old in ('rootProject.file("local.properties")', 'FileInputStream(input)',
                    'buildConfigField("String", "PHONE_PUSH_SECRET", value)'):
            with self.subTest(old=old), self.assertRaises(AssertionError):
                self.assert_credential_free_build(old)

    def test_all_four_consumers_guard_before_authentication_or_network(self):
        folder = ROOT / "android-probe/app/src/main/java/com/nightwatch/etoro/probe"
        cases = {
            "CommandVerifier.kt": ("COMMAND_HMAC_SECRET", "val counter ="),
            "SnapshotPusher.kt": ("PHONE_PUSH_SECRET", "val body = snapshot"),
            "NightwatchProbeMessagingService.kt": ("PHONE_PUSH_SECRET", 'put("token", token)'),
            "TelegramClient.kt": ("TELEGRAM_BOT_TOKEN", "val body = JSONObject"),
        }
        for filename, (key, operation) in cases.items():
            text = (folder / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertNotIn("BuildConfig." + key, text)
                self.assertLess(text.index("RuntimeCredentials.value(RuntimeCredentials.Key." + key), text.index(operation))
                self.assertIn("runtime provisioning required", text)
        telegram = (folder / "TelegramClient.kt").read_text(encoding="utf-8")
        self.assertNotIn("BuildConfig.TELEGRAM_CHAT_ID", telegram)
        self.assertIn("token == null || chatId == null", telegram)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", type=Path, required=True)
    args, rest = parser.parse_known_args()
    SCRATCH = args.scratch
    if not SCRATCH.is_dir():
        parser.error("existing session scratchpad required")
    unittest.main(argv=[__file__, *rest])
