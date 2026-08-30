from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).parents[3] / "scripts" / "public_release.py"
REPOSITORY_ROOT = SCRIPT.parent.parent


def load_release_module() -> ModuleType:
    module_name = "quantsieve_public_release_test_target"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_command(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
    )


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def write_policy(root: Path, includes: list[str]) -> None:
    rendered = ",\n".join(f'  "{item}"' for item in includes)
    (root / "public-release.toml").write_text(
        f"schema_version = 1\n\n[export]\ninclude = [\n{rendered},\n]\n",
        encoding="utf-8",
    )


def seal_inventory(root: Path) -> None:
    paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )
    payload = "".join(f"{path}\n" for path in paths if path != "PUBLIC_RELEASE_MANIFEST.json")
    digest = hashlib.sha256(payload.encode()).hexdigest()
    policy = root / "public-release.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8") + f'inventory_sha256 = "{digest}"\n',
        encoding="utf-8",
    )


def initialize_repository(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.name", "Release Test")
    git(root, "config", "user.email", "release-test@example.test")


def commit_all(root: Path, message: str) -> None:
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", message)


def _dockerignore_matches(pattern: str, relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip("/")
    if "/" not in pattern:
        return any(fnmatchcase(part, pattern) for part in normalized.split("/"))
    candidates = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
    return any(fnmatchcase(normalized, candidate) for candidate in candidates)


def test_repository_dockerignore_excludes_private_context_but_keeps_build_inputs() -> None:
    dockerignore = REPOSITORY_ROOT / ".dockerignore"
    patterns = [
        line
        for raw_line in dockerignore.read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]
    assert patterns
    assert not any(pattern.startswith("!") for pattern in patterns)
    sensitive_paths = (
        ".git/config",
        ".env",
        "apps/api/.env.production",
        "apps/web/.env." + "local",
        "apps/api/private.sqlite3",
        "packages/providers/secrets/token.txt",
        "apps/web/node_modules/example/index.js",
        "apps/web/.next/standalone/server.js",
        ".cache/operator-receipt.json",
        "data/quantsieve.db",
        "backups/quantsieve.db.backup",
        "operator-key.pem",
        "quantsieve-source.tar.gz",
    )
    required_build_inputs = (
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "apps/api/Dockerfile",
        "apps/api/constraints.txt",
        "apps/api/pyproject.toml",
        "apps/api/src/quantsieve_api/main.py",
        "apps/api/src/quantsieve_api/data/demos.json",
        "apps/web/Dockerfile",
        "apps/web/package.json",
        "apps/web/next.config.ts",
        "apps/web/app/page.tsx",
        "packages/engine/pyproject.toml",
        "packages/engine/src/quantsieve_engine/backtest.py",
        "packages/monitor/pyproject.toml",
        "packages/monitor/src/quantsieve_monitor/service.py",
        "packages/providers/pyproject.toml",
        "packages/providers/src/quantsieve_providers/base.py",
    )

    for path in sensitive_paths:
        assert any(_dockerignore_matches(pattern, path) for pattern in patterns), path
    for path in required_build_inputs:
        assert not any(_dockerignore_matches(pattern, path) for pattern in patterns), path

    policy = tomllib.loads((REPOSITORY_ROOT / "public-release.toml").read_text(encoding="utf-8"))
    assert ".dockerignore" in policy["export"]["include"]


def test_repository_prospective_release_files_have_no_scanner_findings() -> None:
    release = load_release_module()
    policy = release.load_policy(REPOSITORY_ROOT / "public-release.toml")
    raw_paths = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        check=True,
    ).stdout
    findings = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        relative_path = release.normalize_relative_path(raw_path.decode("utf-8"))
        absolute_path = REPOSITORY_ROOT / Path(relative_path)
        if not absolute_path.is_file():
            continue
        display_location, metadata_findings = release.path_metadata_findings(relative_path)
        findings.extend(metadata_findings)
        findings.extend(
            release.file_name_findings(
                relative_path,
                location=display_location,
            )
        )
        findings.extend(
            release.content_findings(
                absolute_path.read_bytes(),
                display_location,
                relative_path,
                policy,
            )
        )

    assert not findings, "\n".join(finding.render() for finding in findings)


def test_nas_deployer_preserves_factor_research_resource_limits() -> None:
    deployer = (REPOSITORY_ROOT / "scripts" / "nas-deploy.sh").read_text(encoding="utf-8")
    for setting in (
        "QUANTSIEVE_FACTOR_RESEARCH_MAX_CONCURRENCY",
        "QUANTSIEVE_FACTOR_RESEARCH_PROVIDER_MAX_CONCURRENCY",
        "QUANTSIEVE_FACTOR_RESEARCH_DEADLINE_SECONDS",
    ):
        # Each setting must be both inherited through the explicit whitelist and
        # forwarded to candidate/production containers through optional args.
        assert deployer.count(setting) >= 2


def test_nas_builder_rejects_registry_credentials_and_scans_image_history() -> None:
    builder = (REPOSITORY_ROOT / "scripts" / "nas-build.sh").read_text(encoding="utf-8")
    assert builder.count("require_credential_free_registry_url") >= 3
    assert "QUANTSIEVE_PIP_INDEX_URL" in builder
    assert "QUANTSIEVE_NPM_REGISTRY" in builder
    assert builder.count("assert_image_history_credential_free") >= 3
    assert "docker history --no-trunc" in builder
    assert "could not inspect complete image history" in builder
    assert builder.count("assert_web_provenance_label") >= 2
    assert "Web image source revision label does not match the verified archive" in builder


def test_audit_rejects_non_documentation_network_address(tmp_path: Path) -> None:
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    private_address = "100." + "90.1.2"
    (tmp_path / "README.md").write_text(
        f"server = http://{private_address}:8000\n",
        encoding="utf-8",
    )

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "NETWORK_ADDRESS README.md:1" in result.stderr
    assert private_address not in result.stderr


def test_audit_rejects_bracketed_and_bare_ipv6_without_echoing_values(
    tmp_path: Path,
) -> None:
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    private_address = "fd12" + ":" + "3456" + "::" + "7"
    public_address = "2001" + ":" + "4860" + "::" + "8888"
    (tmp_path / "README.md").write_text(
        f"private={private_address}\npublic=[{public_address}]\n",
        encoding="utf-8",
    )

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 1
    assert result.stderr.count("NETWORK_ADDRESS README.md:") == 2
    assert private_address not in result.stderr
    assert public_address not in result.stderr


def test_audit_allows_loopback_unspecified_and_documentation_ipv6(
    tmp_path: Path,
) -> None:
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    documentation = "2001" + ":" + "db8" + "::" + "42"
    (tmp_path / "README.md").write_text(
        f"loopback=[::1]\nunspecified=::\ndocumentation={documentation}\ncss=.card::before\n",
        encoding="utf-8",
    )

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_audit_scans_path_metadata_without_echoing_sensitive_path(
    tmp_path: Path,
) -> None:
    write_policy(tmp_path, ["docs/", "public-release.toml"])
    private_address = "192.168." + "42.7"
    private_email = "release-owner" + "@company.com"
    sensitive_path = tmp_path / "docs" / f"{private_address}-{private_email}.md"
    sensitive_path.parent.mkdir()
    sensitive_path.write_text("safe content\n", encoding="utf-8")

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "NETWORK_ADDRESS path-metadata@" in result.stderr
    assert "PERSONAL_EMAIL path-metadata@" in result.stderr
    assert private_address not in result.stderr
    assert private_email not in result.stderr


def test_audit_allows_placeholders_and_documentation_addresses(tmp_path: Path) -> None:
    write_policy(tmp_path, [".env.example", "README.md", "public-release.toml"])
    (tmp_path / ".env.example").write_text(
        "QUANTSIEVE_LLM_API_KEY=\nQUANTSIEVE_X_BEARER_TOKEN=...\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "localhost=127.0.0.1\ndocumentation=192.0.2.10\n",
        encoding="utf-8",
    )

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_history_audit_finds_value_removed_from_head(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    private_address = "100." + "90.1.2"
    (tmp_path / "README.md").write_text(
        f"server = http://{private_address}:8000\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "unsafe")
    (tmp_path / "README.md").write_text("server = http://192.0.2.10:8000\n", encoding="utf-8")
    commit_all(tmp_path, "sanitize")

    current = run_command("audit", "--root", str(tmp_path))
    history = run_command("audit", "--root", str(tmp_path), "--history")

    assert current.returncode == 0, current.stderr
    assert history.returncode == 1
    assert "NETWORK_ADDRESS history:README.md@" in history.stderr
    assert private_address not in history.stderr


def test_history_audit_masks_sensitive_path_metadata(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    write_policy(tmp_path, ["docs/", "public-release.toml"])
    private_address = "192.168." + "50.9"
    private_email = "history-owner" + "@company.com"
    sensitive_path = tmp_path / "docs" / f"{private_address}-{private_email}.md"
    sensitive_path.parent.mkdir()
    sensitive_path.write_text("safe content\n", encoding="utf-8")
    commit_all(tmp_path, "sensitive path fixture")

    result = run_command("audit", "--root", str(tmp_path), "--history")

    assert result.returncode == 1
    assert "NETWORK_ADDRESS history:path-metadata@" in result.stderr
    assert "PERSONAL_EMAIL history:path-metadata@" in result.stderr
    assert private_address not in result.stderr
    assert private_email not in result.stderr


def test_history_audit_rejects_reachable_pathless_blob(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    commit_all(tmp_path, "safe source")
    private_address = "10.20." + "30.40"
    blob = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
        input=f"server={private_address}\n",
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    git(tmp_path, "update-ref", "refs/tags/pathless-blob-test", blob)

    result = run_command("audit", "--root", str(tmp_path), "--history")

    assert result.returncode == 1
    assert "UNCLASSIFIED_HISTORY_FILE history:pathless-blob@" in result.stderr
    assert "NETWORK_ADDRESS history:pathless-blob@" in result.stderr
    assert private_address not in result.stderr


def test_build_creates_audited_snapshot_without_git_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "public"
    source.mkdir()
    initialize_repository(source)
    write_policy(
        source,
        ["PUBLIC_RELEASE_MANIFEST.json", "README.md", "public-release.toml"],
    )
    (source / "README.md").write_text("Use http://127.0.0.1 locally.\n", encoding="utf-8")
    seal_inventory(source)
    commit_all(source, "public source")

    result = run_command(
        "build",
        "--root",
        str(source),
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert (output / "README.md").is_file()
    assert not (output / ".git").exists()
    manifest = json.loads((output / "PUBLIC_RELEASE_MANIFEST.json").read_text("utf-8"))
    assert set(manifest["files"]) == {"README.md", "public-release.toml"}


def test_build_blocks_unclassified_tracked_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "public"
    source.mkdir()
    initialize_repository(source)
    write_policy(source, ["README.md", "public-release.toml"])
    (source / "README.md").write_text("safe\n", encoding="utf-8")
    (source / "operator-notes.txt").write_text("not reviewed\n", encoding="utf-8")
    seal_inventory(source)
    commit_all(source, "source")

    result = run_command(
        "build",
        "--root",
        str(source),
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert "UNCLASSIFIED_FILE operator-notes.txt" in result.stderr
    assert not output.exists()


def test_audit_rejects_unpinned_action_without_echoing_value(tmp_path: Path) -> None:
    workflow = ".github/workflows/ci.yml"
    write_policy(tmp_path, [".github/", "public-release.toml"])
    path = tmp_path / workflow
    path.parent.mkdir(parents=True)
    path.write_text(
        "steps:\n  - uses: actions/" + "checkout@v6\n",
        encoding="utf-8",
    )

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 1
    assert f"UNPINNED_ACTION {workflow}:2" in result.stderr
    assert "checkout@v6" not in result.stderr


def test_audit_does_not_skip_dependency_named_directories(tmp_path: Path) -> None:
    write_policy(tmp_path, ["apps/", "public-release.toml"])
    private_address = "100." + "90.1.2"
    path = tmp_path / "apps" / "web" / "node_modules" / "leak.txt"
    path.parent.mkdir(parents=True)
    path.write_text(f"http://{private_address}\n", encoding="utf-8")

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "NETWORK_ADDRESS apps/web/node_modules/leak.txt:1" in result.stderr
    assert private_address not in result.stderr


def test_audit_rejects_default_value_disguised_as_placeholder(tmp_path: Path) -> None:
    write_policy(tmp_path, [".env.example", "public-release.toml"])
    (tmp_path / ".env.example").write_text(
        "QUANTSIEVE_LLM_API_KEY=${QUANTSIEVE_LLM_API_KEY:-not-a-placeholder}\n",
        encoding="utf-8",
    )

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "SECRET_ASSIGNMENT .env.example:1" in result.stderr
    assert "not-a-placeholder" not in result.stderr


def test_audit_rejects_utf16_content_instead_of_skipping_it(tmp_path: Path) -> None:
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    (tmp_path / "README.md").write_text(
        "QUANTSIEVE_LLM_API_KEY=hidden-value\n",
        encoding="utf-16",
    )

    result = run_command("audit", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "BINARY_FILE README.md" in result.stderr
    assert "hidden-value" not in result.stderr


def test_strict_audit_rejects_stale_manifest(tmp_path: Path) -> None:
    write_policy(
        tmp_path,
        ["PUBLIC_RELEASE_MANIFEST.json", "README.md", "public-release.toml"],
    )
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    seal_inventory(tmp_path)
    (tmp_path / "PUBLIC_RELEASE_MANIFEST.json").write_text(
        '{"schema_version": 1, "files": {}}\n',
        encoding="utf-8",
    )

    result = run_command(
        "audit",
        "--root",
        str(tmp_path),
        "--strict-policy",
        "--require-manifest",
    )

    assert result.returncode == 1
    assert "STALE_MANIFEST PUBLIC_RELEASE_MANIFEST.json" in result.stderr


def test_history_audit_scans_commit_messages(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    private_address = "100." + "90.1.2"
    commit_all(tmp_path, f"remove host {private_address}")

    result = run_command("audit", "--root", str(tmp_path), "--history")

    assert result.returncode == 1
    assert "NETWORK_ADDRESS history:commit-message@" in result.stderr
    assert private_address not in result.stderr


def test_history_audit_rejects_personal_commit_and_tag_identities_without_echoing(
    tmp_path: Path,
) -> None:
    initialize_repository(tmp_path)
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    private_address = "100." + "90.1.2"
    private_email = "release-owner" + "@company.com"
    git(tmp_path, "config", "user.name", f"Release Owner {private_address}")
    git(tmp_path, "config", "user.email", private_email)
    commit_all(tmp_path, "public source")
    git(tmp_path, "tag", "-a", "v1.0.0", "-m", "public release")

    result = run_command("audit", "--root", str(tmp_path), "--history")

    assert result.returncode == 1
    for location in ("commit-author@", "commit-committer@", "tagger@"):
        assert f"PERSONAL_EMAIL history:{location}" in result.stderr
        assert f"NETWORK_ADDRESS history:{location}" in result.stderr
    assert private_email not in result.stderr
    assert private_address not in result.stderr


def test_history_audit_allows_example_and_github_noreply_identities(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    write_policy(tmp_path, ["README.md", "public-release.toml"])
    (tmp_path / "README.md").write_text("version 1\n", encoding="utf-8")
    commit_all(tmp_path, "initial public source")

    git(tmp_path, "config", "user.name", "QuantSieve Release Bot")
    git(
        tmp_path,
        "config",
        "user.email",
        "12345678+quantsieve-release@users.noreply.github.com",
    )
    (tmp_path / "README.md").write_text("version 2\n", encoding="utf-8")
    commit_all(tmp_path, "public source")
    git(tmp_path, "tag", "-a", "v1.0.0", "-m", "public release")

    git(tmp_path, "config", "user.name", "GitHub")
    git(tmp_path, "config", "user.email", "noreply@github.com")
    (tmp_path / "README.md").write_text("version 3\n", encoding="utf-8")
    commit_all(tmp_path, "GitHub merge commit")

    result = run_command("audit", "--root", str(tmp_path), "--history")

    assert result.returncode == 0, result.stderr


def test_git_audit_rejects_embedded_repository_mode(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    vendor = parent / "vendor"
    parent.mkdir()
    initialize_repository(parent)
    write_policy(parent, ["README.md", "public-release.toml", "vendor"])
    (parent / "README.md").write_text("safe\n", encoding="utf-8")
    vendor.mkdir()
    initialize_repository(vendor)
    (vendor / "README.md").write_text("nested\n", encoding="utf-8")
    commit_all(vendor, "nested")
    commit_all(parent, "parent")

    result = run_command("audit", "--root", str(parent))

    assert result.returncode == 1
    assert "UNSAFE_GIT_MODE vendor" in result.stderr
