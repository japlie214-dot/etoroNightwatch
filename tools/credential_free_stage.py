"""Stage reviewed sources or inventory tracked paths; never run Gradle.

The approval JSON is a list of {"path": "repo/relative/path", "sha256": "..."}.
Hashes attest reviewed bytes, not that a file is public: the operator must approve
that separately. --inventory reads git path metadata only, never file contents or
approvals. No default manifest, environment lookup, recursive copy or cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


BUILD_FILES = {
    "android-probe/build.gradle.kts",
    "android-probe/settings.gradle.kts",
    "android-probe/app/build.gradle.kts",
}
SOURCE_ROOTS = (
    "android-probe/app/src/main/",
    "android-probe/app/src/test/",
    "app/src/main/python/",
)
DENIED_PARTS = {
    "build", ".gradle", ".git", ".idea", "__pycache__", "assets",
    "secrets", "private", "runtime", "nightwatch_demo", "node_modules",
}


def source_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name:
        raise ValueError("approval requires an exact source path")
    parts = name.split("/")
    if any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part)
           or part.endswith(".") or part.casefold() in DENIED_PARTS
           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", part)
           for part in parts):
        raise ValueError("unsafe source path")
    path = PurePosixPath(name)
    package_sql = name == "app/src/main/python/nightwatch/db/schema.sql" or bool(
        re.fullmatch(r"app/src/main/python/nightwatch/db/migrations/[0-9]+_[A-Za-z0-9_]+\.sql", name)
    )
    if name not in BUILD_FILES and not package_sql:
        if not name.startswith(SOURCE_ROOTS) or path.suffix not in {".kt", ".xml", ".py"}:
            raise ValueError("not an approved source category")
    if any(word in part.casefold() for part in parts
           for word in ("secret", "keystore", "credentials", ".env", "google-services")):
        raise ValueError("private/configuration path forbidden")
    return path


def no_links(path: Path) -> None:
    # Windows junctions are reparse points too; resolve() alone would hide them.
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("linked paths forbidden")
        if parent.exists() and getattr(parent.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("reparse points forbidden")


def stage(root: Path, destination: Path, approvals: list[dict]) -> int:
    root, destination = Path(os.path.abspath(root)), Path(os.path.abspath(destination))
    no_links(root)
    no_links(destination)
    if not root.is_dir() or destination.exists():
        raise ValueError("source root must exist; destination must be new")
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("clean room must be separate from source workspace")
    if not isinstance(approvals, list) or not approvals:
        raise ValueError("individual public-source approvals required")
    planned = []
    seen = set()
    for entry in approvals:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("approval must contain only path and sha256")
        relative = source_path(entry["path"])
        digest = entry["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("reviewed SHA-256 required")
        if str(relative).casefold() in seen:
            raise ValueError("duplicate source path")
        seen.add(str(relative).casefold())
        source = root.joinpath(*relative.parts)
        no_links(source)
        if not source.is_file() or source.stat().st_nlink != 1:
            raise ValueError("only ordinary, non-hardlinked source files allowed")
        planned.append((source, relative, digest))
    # Validate every name before reading any approved content.
    verified = []
    for source, relative, digest in planned:
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("source differs from approved bytes")
        verified.append((relative, data))
    destination.mkdir(parents=True, exist_ok=False)
    for relative, data in verified:
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(data)
    return len(verified)


def inventory(tracked: list[str]) -> dict:
    """Describe indexed paths only; presence is neither disk verification nor approval."""
    names = sorted(set(tracked))
    exact = sorted(BUILD_FILES | {
        "android-probe/app/src/main/AndroidManifest.xml",
        "app/src/main/python/nightwatch/__init__.py",
        "app/src/main/python/nightwatch/db/schema.sql",
    })
    groups = {
        "build_definitions": sorted(BUILD_FILES & set(names)),
        "android_manifest": [p for p in names if p == "android-probe/app/src/main/AndroidManifest.xml"],
        "android_kotlin": [p for p in names if p.startswith("android-probe/app/src/main/java/") and p.endswith(".kt")],
        "android_resources": [p for p in names if p.startswith("android-probe/app/src/main/res/") and p.endswith(".xml")],
        "probe_python": [p for p in names if p.startswith("android-probe/app/src/main/python/") and p.endswith(".py")],
        "nightwatch_python": [p for p in names if p.startswith("app/src/main/python/nightwatch/") and p.endswith(".py")],
        "nightwatch_sql_schema": [p for p in names if p == "app/src/main/python/nightwatch/db/schema.sql"],
        "nightwatch_sql_migrations": [p for p in names if p.startswith("app/src/main/python/nightwatch/db/migrations/") and p.endswith(".sql")],
    }
    rejected = []
    for group in groups.values():
        for name in group:
            try:
                source_path(name)
            except ValueError as exc:
                rejected.append({"path": name, "reason": str(exc)})
    return {
        "mode": "metadata-only-dry-run",
        "build_ready": False,
        "approval": "none granted; no hashes calculated; every input needs content review",
        "presence_basis": "git index paths only, not filesystem existence or dependency closure",
        "source_groups": groups,
        "missing_source_groups": [name for name, paths in groups.items() if not paths],
        "missing_required_paths": [p for p in exact if p not in names],
        "staging_rule_rejections": rejected,
        "optional_test_paths": [p for p in names if p.startswith("android-probe/app/src/test/")],
        "excluded_build_state_paths": [p for p in names if p in {
            "android-probe/gradle.properties", "android-probe/local.properties.example",
            "android-probe/gradlew", "android-probe/gradlew.bat",
            "android-probe/gradle/wrapper/gradle-wrapper.jar",
            "android-probe/gradle/wrapper/gradle-wrapper.properties",
        }],
        "unsigned_compile_prerequisites": {
            "tool_versions_required_not_probed": {
                "Gradle": "8.11.1 (README; wrapper contents not read)",
                "JDK": "17", "Android compile/target SDK": "36; min SDK 24; ABI arm64-v8a",
                "Android build-tools": "not pinned in reviewed definitions; approve AGP resolution",
                "host Python": "3.13; replace historical absolute buildPython path in isolated copy",
                "AGP": "8.9.0", "Kotlin plugin": "2.0.21", "Chaquopy": "17.0.0",
                "Google Services plugin": "4.4.2", "Firebase BOM": "33.5.1",
            },
            "public_dependency_inputs": {
                "repositories": ["google()", "mavenCentral()", "gradlePluginPortal()", "https://chaquo.com/maven"],
                "maven": "review dependencies in android-probe/app/build.gradle.kts and approve resolved public artifacts; no existing caches",
                "pip": ["tzdata (unpinned)", "numpy (unpinned)", "pandas (unpinned)",
                        "httpx==0.27.2", "exchange_calendars==4.11.1"],
            },
            "unresolved_inputs": [
                "No per-file reviewed source approval manifest supplied; inventory is not approval.",
                "Independently supplied, reviewed public Firebase configuration for the app plugin; never read/copy existing android-probe/app/google-services.json. Stager deliberately rejects that path.",
                "Reviewed minimal AndroidX project configuration required; existing android-probe/gradle.properties is excluded and unread. Stager does not provision properties.",
                "Approved isolated Gradle/JDK/SDK/Python executables and public dependencies; no original workspace, user home, caches, proxy/pip configuration or signing-store access.",
                "Source-set paths in the staged layout must preserve android-probe/ and app/ siblings; approve the host Python selection before running Gradle.",
            ],
            "artifact": "unsigned release APK only; no debug signing, install or runtime-readiness claim",
        },
        "compatible_external_signing_gate": "Separate authorized signer must preserve installed certificate/package and upgrade version; do not inspect/export signing material. Unsigned compilation cannot prove compatibility.",
        "runtime_provisioning_gate": "Trusted restart-safe provider and Firebase delivery must be wired and verified independently before replacement. Renaming/staging credentials is not protection.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--inventory", action="store_true", help="list tracked input paths and unresolved gates without reading contents")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--approvals", type=Path)
    args = parser.parse_args()
    if args.inventory:
        if args.destination or args.approvals:
            parser.error("inventory cannot stage files or read approvals")
        result = subprocess.run(
            ["git", "-C", str(args.root), "ls-files", "-z", "--", "android-probe", "app/src/main/python"],
            check=True, capture_output=True,
        )
        paths = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        print(json.dumps(inventory([p for p in paths if p]), indent=2))
        return
    if args.destination is None or args.approvals is None:
        parser.error("staging requires --destination and --approvals")
    approvals = json.loads(args.approvals.read_text(encoding="utf-8"))
    count = stage(args.root, args.destination, approvals)
    print(f"Staged {count} approved source files. NOT build-ready: tooling and runtime provisioning require approval.")


if __name__ == "__main__":
    main()
