#!/usr/bin/env python3
"""Build and audit a clean-history QuantSieve public snapshot.

This tool never pushes, creates a remote, or rewrites the source repository.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_TEXT_BYTES = 5 * 1024 * 1024
MANIFEST_NAME = "PUBLIC_RELEASE_MANIFEST.json"

WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
SERVER_PATH = re.compile(
    r"(?:" + r"(?i:" + r"/mnt/(?:user|pool|cache)(?:/|\b))"
    r"|/home/[^/\s]+/(?:Desktop|Documents|Downloads|projects?)/"
    r"|/Users/[^/\s]+/(?:Desktop|Documents|Downloads|Library)/"
    r"|\\\\[A-Za-z0-9][A-Za-z0-9.-]*\\[A-Za-z0-9$][^\\\s]*"
    r"|(?i:(?:ssh|scp)[ \t]+[^\s@]+@[^\s:]+:)"
    r")"
)
PRIVATE_REFERENCE = re.compile(
    r"(?i)(?:"
    + r"(?:^|[/\\])_"
    + "PRIVATE"
    + r"(?:[/\\]|$)"
    + r"|quantsieve-"
    + "pro"
    + r"(?:[/\\]|\.git\b)"
    + r")"
)
IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
IPV6 = re.compile(
    r"(?i)(?<![0-9A-Z_:.])(?P<value>(?:[0-9A-F]{0,4}:){2,7}[0-9A-F]{0,4})"
    r"(?![0-9A-Z_:.])"
)
EMAIL = re.compile(r"(?i)(?<![A-Z0-9._%+-])([A-Z0-9._%+-]+)@([A-Z0-9.-]+\.[A-Z]{2,})")
PRIVATE_HOSTNAME = re.compile(
    r"(?i)\b[A-Z0-9](?:[A-Z0-9-]{0,62}\.)+"
    r"(?:local|lan|internal|home|ts\.net)\b"
)
REMOTE_ACTION = re.compile(
    r"(?im)^[ \t]*-[ \t]*uses:[ \t]*(?!\./)(?P<action>[^@\s]+)@(?P<ref>[^\s#]+)"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
CONNECTION_CREDENTIAL = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp(?:s)?)"
    r"://[^/\s:@]+:[^/\s@]+@"
)
STRONG_SECRET_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)
ENV_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:export[ \t]+)?"
    r"[A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|BEARER_TOKEN|SECRET|PASSWORD|PRIVATE_KEY)"
    r"[ \t]*=[ \t]*(?P<value>[^\s#]*)"
)
STRUCTURED_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    ["'](?:api[_-]?key|access[_-]?token|bearer[_-]?token|secret|password|private[_-]?key)["']
    \s*:\s*
    ["'](?P<value>[^"']+)["']
    """
)
PLACEHOLDERS = {
    "",
    "...",
    "changeme",
    "example",
    "none",
    "null",
    "placeholder",
    "redacted",
    "replace-me",
    "todo",
    "your-key-here",
    "your-token-here",
}
FORBIDDEN_EXACT_FILENAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "kubeconfig",
}
FORBIDDEN_SUFFIXES = {
    ".7z",
    ".bak",
    ".backup",
    ".db",
    ".gz",
    ".jks",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".zip",
}


class ReleaseError(RuntimeError):
    """A safe release precondition was not satisfied."""


@dataclass(frozen=True)
class Policy:
    schema_version: int
    includes: tuple[str, ...]
    binary_sha256: tuple[tuple[str, tuple[str, ...]], ...]
    inventory_sha256: str | None

    def allows(self, relative_path: str) -> bool:
        normalized = normalize_relative_path(relative_path)
        return any(
            normalized.startswith(rule) if rule.endswith("/") else normalized == rule
            for rule in self.includes
        )

    def allows_binary(self, relative_path: str, digest: str) -> bool:
        normalized = normalize_relative_path(relative_path)
        return any(
            normalized == path and digest in allowed_hashes
            for path, allowed_hashes in self.binary_sha256
        )


@dataclass(frozen=True)
class GitEntry:
    mode: str
    object_type: str
    object_id: str
    path: str


@dataclass(frozen=True)
class Finding:
    code: str
    location: str
    line: int | None
    message: str

    def render(self) -> str:
        suffix = f":{self.line}" if self.line is not None else ""
        return f"{self.code} {self.location}{suffix} - {self.message}"


def normalize_relative_path(value: str) -> str:
    parsed = PurePosixPath(value.replace("\\", "/"))
    normalized = parsed.as_posix()
    if (
        normalized in {"", "."}
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
        raise ReleaseError(f"unsafe relative path: path-metadata@{digest}")
    return normalized


def load_policy(path: Path) -> Policy:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        schema_version = int(raw["schema_version"])
        includes = tuple(str(item) for item in raw["export"]["include"])
        raw_inventory_sha256 = raw["export"].get("inventory_sha256")
        raw_binary_hashes = raw.get("audit", {}).get("binary_sha256", {})
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"cannot load release policy {path}: {exc}") from exc
    if schema_version != 1:
        raise ReleaseError(f"unsupported release policy schema: {schema_version}")
    if not includes:
        raise ReleaseError("release allowlist is empty")
    normalized_rules: list[str] = []
    for rule in includes:
        is_prefix = rule.endswith("/")
        normalized = normalize_relative_path(rule.rstrip("/"))
        normalized_rules.append(f"{normalized}/" if is_prefix else normalized)
    if len(normalized_rules) != len(set(normalized_rules)):
        raise ReleaseError("release allowlist contains duplicate entries")
    inventory_sha256 = (
        str(raw_inventory_sha256).lower() if raw_inventory_sha256 is not None else None
    )
    if inventory_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", inventory_sha256) is None:
        raise ReleaseError("export.inventory_sha256 must be a full SHA-256")
    if not isinstance(raw_binary_hashes, dict):
        raise ReleaseError("audit.binary_sha256 must be a table")
    binary_hashes: list[tuple[str, tuple[str, ...]]] = []
    for raw_path, raw_hashes in raw_binary_hashes.items():
        binary_path = normalize_relative_path(str(raw_path))
        if not isinstance(raw_hashes, list) or not raw_hashes:
            raise ReleaseError(f"binary hash allowlist must be a non-empty array: {binary_path}")
        hashes = tuple(str(item).lower() for item in raw_hashes)
        if any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in hashes):
            raise ReleaseError(f"invalid SHA-256 in binary allowlist: {binary_path}")
        if len(hashes) != len(set(hashes)):
            raise ReleaseError(f"duplicate binary hash in allowlist: {binary_path}")
        binary_hashes.append((binary_path, hashes))
    return Policy(
        schema_version=schema_version,
        includes=tuple(normalized_rules),
        binary_sha256=tuple(sorted(binary_hashes)),
        inventory_sha256=inventory_sha256,
    )


def run_git(root: Path, arguments: Sequence[str], *, text: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", errors="replace")
        raise ReleaseError(f"git {' '.join(arguments)} failed: {stderr.strip()}")
    stdout = completed.stdout
    if text:
        assert isinstance(stdout, str)
        return stdout
    assert isinstance(stdout, bytes)
    return stdout


def ensure_git_root(root: Path) -> None:
    output = run_git(root, ["rev-parse", "--show-toplevel"], text=True)
    assert isinstance(output, str)
    actual_root = Path(output.strip()).resolve()
    if actual_root != root.resolve():
        raise ReleaseError(f"--root must be the Git repository top level: {actual_root}")


def resolved_commit(root: Path, ref: str) -> str:
    output = run_git(root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], text=True)
    assert isinstance(output, str)
    commit = output.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ReleaseError(f"Git did not resolve a full commit for ref: {ref}")
    return commit


def git_entries(root: Path, ref: str) -> list[GitEntry]:
    raw = run_git(root, ["ls-tree", "-r", "-z", "--full-tree", ref])
    assert isinstance(raw, bytes)
    entries: list[GitEntry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        path = normalize_relative_path(raw_path.decode("utf-8"))
        entries.append(GitEntry(mode, object_type, object_id, path))
    return entries


def git_is_clean(root: Path) -> bool:
    output = run_git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    assert isinstance(output, bytes)
    return not output.strip()


def policy_findings(paths: Iterable[str], policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(set(paths)):
        if not policy.allows(path):
            display_location, _ = path_metadata_findings(path)
            findings.append(
                Finding(
                    "UNCLASSIFIED_FILE",
                    display_location,
                    None,
                    "tracked/output file is not present in the public allowlist",
                )
            )
    return findings


def path_inventory_sha256(paths: Iterable[str]) -> str:
    normalized_paths = {normalize_relative_path(path) for path in paths}
    normalized_paths.discard(MANIFEST_NAME)
    normalized = sorted(normalized_paths)
    payload = "".join(f"{path}\n" for path in normalized).encode()
    return hashlib.sha256(payload).hexdigest()


def inventory_findings(
    paths: Iterable[str],
    policy: Policy,
    *,
    required: bool,
) -> list[Finding]:
    if policy.inventory_sha256 is None:
        if not required:
            return []
        return [
            Finding(
                "MISSING_INVENTORY",
                "public-release.toml",
                None,
                "strict release policy requires an approved file-inventory digest",
            )
        ]
    if path_inventory_sha256(paths) == policy.inventory_sha256:
        return []
    return [
        Finding(
            "INVENTORY_MISMATCH",
            "public-release.toml",
            None,
            "tracked/output file inventory differs from the approved digest",
        )
    ]


def file_name_findings(path: str, *, location: str | None = None) -> list[Finding]:
    name = PurePosixPath(path).name
    lowered = name.lower()
    display_location = location or path
    findings: list[Finding] = []
    if lowered in FORBIDDEN_EXACT_FILENAMES and lowered != ".env.example":
        findings.append(
            Finding(
                "FORBIDDEN_FILE",
                display_location,
                None,
                "credential or machine-local file name",
            )
        )
    if any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        findings.append(
            Finding(
                "FORBIDDEN_FILE",
                display_location,
                None,
                "secret, database, or backup file suffix",
            )
        )
    if re.search(r"\.(?:db|sqlite|sqlite3)-(?:journal|shm|wal)$", lowered):
        findings.append(Finding("FORBIDDEN_FILE", display_location, None, "database sidecar file"))
    if lowered.startswith(".env.") and lowered != ".env.example":
        findings.append(
            Finding("FORBIDDEN_FILE", display_location, None, "environment-specific file")
        )
    return findings


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_placeholder(raw_value: str) -> bool:
    value = raw_value.strip().strip("\"'").strip()
    lowered = value.lower()
    return (
        lowered in PLACEHOLDERS
        or re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*\}", value) is not None
        or re.fullmatch(r"<YOUR_[A-Z][A-Z0-9_]*>", value) is not None
    )


def _is_allowed_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    allowed_networks = (
        ipaddress.ip_network("0.0.0.0/32"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("224.0.0.0/4"),
        ipaddress.ip_network("255.255.255.255/32"),
    )
    return any(address in network for network in allowed_networks)


def _is_allowed_ipv6(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    documentation = ipaddress.ip_network("2001:db8::/32")
    return address.is_loopback or address.is_unspecified or address in documentation


def _is_allowed_email_domain(domain: str) -> bool:
    lowered = domain.lower()
    return lowered in {
        "example.com",
        "example.net",
        "example.org",
        "example.test",
        "users.noreply.github.com",
    }


def _is_allowed_email(local_part: str, domain: str) -> bool:
    return _is_allowed_email_domain(domain) or (
        local_part.lower() == "noreply" and domain.lower() == "github.com"
    )


def text_findings(text: str, location: str) -> list[Finding]:
    findings: list[Finding] = []

    def add_matches(pattern: re.Pattern[str], code: str, message: str) -> None:
        for match in pattern.finditer(text):
            findings.append(Finding(code, location, _line_number(text, match.start()), message))

    add_matches(PRIVATE_KEY, "PRIVATE_KEY", "private-key material is forbidden")
    add_matches(
        CONNECTION_CREDENTIAL,
        "CONNECTION_CREDENTIAL",
        "connection string contains inline credentials",
    )
    add_matches(
        WINDOWS_ABSOLUTE_PATH,
        "WORKSTATION_PATH",
        "machine-specific Windows path is forbidden",
    )
    add_matches(
        SERVER_PATH,
        "SERVER_PATH",
        "operator, mount, network-share, or remote-shell path is forbidden",
    )
    add_matches(
        PRIVATE_REFERENCE,
        "PRIVATE_REFERENCE",
        "private repository or planning reference is forbidden",
    )
    add_matches(
        PRIVATE_HOSTNAME,
        "PRIVATE_HOSTNAME",
        "machine-local or private-network hostname is forbidden",
    )
    for pattern in STRONG_SECRET_PATTERNS:
        add_matches(pattern, "SECRET_TOKEN", "credential-like token is forbidden")
    for match in ENV_ASSIGNMENT_PATTERN.finditer(text):
        if not _is_placeholder(match.group("value")):
            findings.append(
                Finding(
                    "SECRET_ASSIGNMENT",
                    location,
                    _line_number(text, match.start()),
                    "non-placeholder secret environment value",
                )
            )
    for match in STRUCTURED_ASSIGNMENT_PATTERN.finditer(text):
        if not _is_placeholder(match.group("value")):
            findings.append(
                Finding(
                    "SECRET_ASSIGNMENT",
                    location,
                    _line_number(text, match.start()),
                    "non-placeholder structured secret value",
                )
            )
    for match in IPV4.finditer(text):
        value = match.group(0)
        if not _is_allowed_ipv4(value):
            findings.append(
                Finding(
                    "NETWORK_ADDRESS",
                    location,
                    _line_number(text, match.start()),
                    "numeric address is not localhost or an RFC documentation address",
                )
            )
    for match in IPV6.finditer(text):
        try:
            address = ipaddress.ip_address(match.group("value"))
        except ValueError:
            continue
        if not _is_allowed_ipv6(str(address)):
            findings.append(
                Finding(
                    "NETWORK_ADDRESS",
                    location,
                    _line_number(text, match.start()),
                    "IPv6 address is not localhost, unspecified, or RFC documentation",
                )
            )
    for match in EMAIL.finditer(text):
        if not _is_allowed_email(match.group(1), match.group(2)):
            findings.append(
                Finding(
                    "PERSONAL_EMAIL",
                    location,
                    _line_number(text, match.start()),
                    "non-example email address is forbidden",
                )
            )
    for match in REMOTE_ACTION.finditer(text):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", match.group("ref")):
            findings.append(
                Finding(
                    "UNPINNED_ACTION",
                    location,
                    _line_number(text, match.start()),
                    "third-party GitHub Action must be pinned to a full commit SHA",
                )
            )
    return findings


def path_metadata_findings(
    path: str,
    *,
    prefix: str = "",
    suffix: str = "",
) -> tuple[str, list[Finding]]:
    """Scan a path without ever echoing sensitive path text in a finding."""

    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    opaque_location = f"{prefix}path-metadata@{digest}{suffix}"
    findings = text_findings(path, opaque_location)
    display_location = opaque_location if findings else f"{prefix}{path}{suffix}"
    return display_location, findings


def content_findings(
    content: bytes,
    location: str,
    relative_path: str,
    policy: Policy,
) -> list[Finding]:
    if len(content) > MAX_TEXT_BYTES:
        return [
            Finding(
                "OVERSIZE_FILE",
                location,
                None,
                f"file exceeds the {MAX_TEXT_BYTES}-byte public audit limit",
            )
        ]
    if b"\0" in content:
        digest = hashlib.sha256(content).hexdigest()
        if policy.allows_binary(relative_path, digest):
            return []
        return [
            Finding(
                "BINARY_FILE",
                location,
                None,
                "binary or NUL-containing content is not pinned in the release policy",
            )
        ]
    text = content.decode("utf-8", errors="replace")
    return text_findings(text, location)


def iter_tree_files(root: Path) -> Iterable[tuple[str, bytes]]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            relative = path.relative_to(root).as_posix()
            display_location, _ = path_metadata_findings(relative)
            raise ReleaseError(
                f"symbolic links are not allowed in a public snapshot: {display_location}"
            )
        if path.is_file():
            relative = normalize_relative_path(path.relative_to(root).as_posix())
            yield relative, path.read_bytes()


def manifest_findings(
    files: Iterable[tuple[str, bytes]],
    *,
    required: bool,
) -> list[Finding]:
    file_map = dict(files)
    raw_manifest = file_map.pop(MANIFEST_NAME, None)
    if raw_manifest is None:
        if not required:
            return []
        return [
            Finding(
                "MISSING_MANIFEST",
                MANIFEST_NAME,
                None,
                "public repository/candidate requires a release manifest",
            )
        ]
    try:
        payload = json.loads(raw_manifest.decode("utf-8"))
        recorded = payload["files"]
        schema_version = int(payload["schema_version"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return [
            Finding(
                "INVALID_MANIFEST",
                MANIFEST_NAME,
                None,
                "release manifest is malformed",
            )
        ]
    if schema_version != 1 or not isinstance(recorded, dict):
        return [
            Finding(
                "INVALID_MANIFEST",
                MANIFEST_NAME,
                None,
                "release manifest schema is unsupported",
            )
        ]
    expected = {path: hashlib.sha256(content).hexdigest() for path, content in file_map.items()}
    normalized_recorded = {str(path): str(digest).lower() for path, digest in recorded.items()}
    if normalized_recorded == expected:
        return []
    return [
        Finding(
            "STALE_MANIFEST",
            MANIFEST_NAME,
            None,
            "manifest file set or content hashes do not match the public tree",
        )
    ]


def audit_tree(
    root: Path,
    policy: Policy,
    *,
    strict_policy: bool = False,
    require_manifest: bool = False,
) -> list[Finding]:
    files = list(iter_tree_files(root))
    paths = [path for path, _ in files]
    findings = policy_findings(paths, policy)
    findings.extend(inventory_findings(paths, policy, required=strict_policy))
    findings.extend(manifest_findings(files, required=require_manifest))
    for path, content in files:
        display_location, metadata_findings = path_metadata_findings(path)
        findings.extend(metadata_findings)
        findings.extend(file_name_findings(path, location=display_location))
        findings.extend(content_findings(content, display_location, path, policy))
    return findings


def audit_git_worktree(
    root: Path,
    policy: Policy,
    *,
    strict_policy: bool = False,
    require_manifest: bool = False,
) -> list[Finding]:
    ensure_git_root(root)
    if not git_is_clean(root):
        raise ReleaseError("Git audit requires a clean worktree and index")
    entries = git_entries(root, "HEAD")
    paths = [entry.path for entry in entries]
    findings = policy_findings(paths, policy)
    findings.extend(inventory_findings(paths, policy, required=strict_policy))
    files: list[tuple[str, bytes]] = []
    for entry in entries:
        path = entry.path
        display_location, metadata_findings = path_metadata_findings(path)
        findings.extend(metadata_findings)
        findings.extend(file_name_findings(path, location=display_location))
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            findings.append(
                Finding(
                    "UNSAFE_GIT_MODE",
                    display_location,
                    None,
                    "only regular non-symlink blobs are allowed",
                )
            )
            continue
        content = _read_git_blob(root, entry.object_id)
        files.append((path, content))
        findings.extend(content_findings(content, display_location, path, policy))
    findings.extend(manifest_findings(files, required=require_manifest))
    return findings


def iter_history_blobs(root: Path) -> Iterable[tuple[str, str | None, bytes]]:
    raw_objects = run_git(root, ["rev-list", "--objects", "--all"])
    assert isinstance(raw_objects, bytes)
    object_paths: dict[str, list[str]] = {}
    for raw_line in raw_objects.splitlines():
        parts = raw_line.decode("utf-8", errors="replace").split(" ", 1)
        if not parts or not parts[0]:
            continue
        object_id = parts[0]
        paths = object_paths.setdefault(object_id, [])
        if len(parts) != 2:
            continue
        raw_path = parts[1]
        if not raw_path:
            continue
        path = normalize_relative_path(raw_path)
        if path not in paths:
            paths.append(path)

    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        for object_id, paths in object_paths.items():
            process.stdin.write(f"{object_id}\n".encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", errors="replace").strip()
            header_parts = header.split()
            if len(header_parts) != 3:
                raise ReleaseError(f"unexpected git cat-file response: {header}")
            _, object_type, raw_size = header_parts
            size = int(raw_size)
            content = process.stdout.read(size)
            terminator = process.stdout.read(1)
            if terminator != b"\n":
                raise ReleaseError("git cat-file returned a malformed object stream")
            if object_type == "blob":
                if paths:
                    for path in paths:
                        yield object_id, path, content
                else:
                    yield object_id, None, content
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait(timeout=10)
        if process.returncode != 0:
            assert process.stderr is not None
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            raise ReleaseError(f"git cat-file failed: {stderr.strip()}")


def audit_history(root: Path, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    blobs = list(iter_history_blobs(root))
    history_paths = sorted({path for _, path, _ in blobs if path is not None})
    for finding in policy_findings(history_paths, policy):
        findings.append(
            Finding(
                "UNCLASSIFIED_HISTORY_FILE",
                f"history:{finding.location}",
                None,
                finding.message,
            )
        )
    for object_id, path, content in blobs:
        if path is None:
            location = f"history:pathless-blob@{object_id[:12]}"
            findings.append(
                Finding(
                    "UNCLASSIFIED_HISTORY_FILE",
                    location,
                    None,
                    "reachable Git blob has no reviewed release path",
                )
            )
            findings.extend(
                content_findings(
                    content,
                    location,
                    f"pathless-blob-{object_id}.bin",
                    policy,
                )
            )
            continue
        location, metadata_findings = path_metadata_findings(
            path,
            prefix="history:",
            suffix=f"@{object_id[:12]}",
        )
        findings.extend(metadata_findings)
        findings.extend(file_name_findings(path, location=location))
        findings.extend(content_findings(content, location, path, policy))
    findings.extend(audit_history_metadata(root, policy))
    return findings


def _nul_pairs(raw: bytes) -> Iterable[tuple[str, bytes]]:
    fields = raw.split(b"\0")
    for index in range(0, len(fields) - 1, 2):
        identifier = fields[index].decode("utf-8", errors="replace").strip()
        if identifier:
            yield identifier, fields[index + 1]


def _nul_line_records(
    raw: bytes,
    *,
    field_count: int,
    metadata_type: str,
) -> Iterable[tuple[bytes, ...]]:
    for raw_line in raw.split(b"\n"):
        if not raw_line:
            continue
        fields = tuple(raw_line.rstrip(b"\r").split(b"\0"))
        if len(fields) != field_count:
            raise ReleaseError(f"Git returned malformed {metadata_type} metadata")
        yield fields


def audit_history_metadata(root: Path, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    raw_commits = run_git(root, ["log", "--all", "--format=%H%x00%B%x00"])
    assert isinstance(raw_commits, bytes)
    for commit, message in _nul_pairs(raw_commits):
        findings.extend(
            content_findings(
                message,
                f"history:commit-message@{commit[:12]}",
                "README.md",
                policy,
            )
        )

    raw_commit_identities = run_git(
        root,
        ["log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce"],
    )
    assert isinstance(raw_commit_identities, bytes)
    for raw_record in _nul_line_records(
        raw_commit_identities,
        field_count=5,
        metadata_type="commit identity",
    ):
        raw_commit, author_name, author_email, committer_name, committer_email = raw_record
        commit = raw_commit.decode("ascii", errors="replace").strip()
        identities = (
            ("author", author_name + b" <" + author_email + b">"),
            ("committer", committer_name + b" <" + committer_email + b">"),
        )
        for role, identity in identities:
            findings.extend(
                content_findings(
                    identity,
                    f"history:commit-{role}@{commit[:12]}",
                    "README.md",
                    policy,
                )
            )

    raw_tags = run_git(
        root,
        ["for-each-ref", "refs/tags", "--format=%(objectname)%00%(contents)%00"],
    )
    assert isinstance(raw_tags, bytes)
    for object_id, message in _nul_pairs(raw_tags):
        findings.extend(
            content_findings(
                message,
                f"history:tag-message@{object_id[:12]}",
                "README.md",
                policy,
            )
        )

    raw_tag_identities = run_git(
        root,
        [
            "for-each-ref",
            "refs/tags",
            "--format=%(objectname)%00%(objecttype)%00%(taggername)%00%(taggeremail)",
        ],
    )
    assert isinstance(raw_tag_identities, bytes)
    for raw_record in _nul_line_records(
        raw_tag_identities,
        field_count=4,
        metadata_type="tag identity",
    ):
        raw_object_id, object_type, tagger_name, tagger_email = raw_record
        if object_type != b"tag":
            continue
        object_id = raw_object_id.decode("ascii", errors="replace").strip()
        findings.extend(
            content_findings(
                tagger_name + b" " + tagger_email,
                f"history:tagger@{object_id[:12]}",
                "README.md",
                policy,
            )
        )

    raw_refs = run_git(root, ["for-each-ref", "--format=%(refname)"])
    assert isinstance(raw_refs, bytes)
    findings.extend(text_findings(raw_refs.decode("utf-8", errors="replace"), "history:refs"))
    return findings


def print_findings(findings: Sequence[Finding]) -> None:
    for finding in sorted(findings, key=lambda item: (item.location, item.line or 0, item.code)):
        print(finding.render(), file=sys.stderr)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(root: Path) -> None:
    hashes = {
        path: sha256_file(root / Path(path))
        for path, _ in iter_tree_files(root)
        if path != MANIFEST_NAME
    }
    payload = {
        "schema_version": 1,
        "files": hashes,
    }
    (root / MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_git_blob(root: Path, object_id: str) -> bytes:
    content = run_git(root, ["cat-file", "blob", object_id])
    assert isinstance(content, bytes)
    return content


def _safe_staging_path(output: Path, root: Path) -> Path:
    resolved_output = output.resolve()
    resolved_root = root.resolve()
    if resolved_output == resolved_root or resolved_root in resolved_output.parents:
        raise ReleaseError("public output must be outside the source repository")
    if resolved_output.exists():
        raise ReleaseError(f"public output already exists: {resolved_output}")
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    return resolved_output.parent / f".{resolved_output.name}.staging-{uuid.uuid4().hex}"


def build_snapshot(root: Path, policy: Policy, output: Path, ref: str) -> int:
    ensure_git_root(root)
    if not git_is_clean(root):
        raise ReleaseError(
            "source repository is dirty; commit or remove pending files before export"
        )
    if resolved_commit(root, ref) != resolved_commit(root, "HEAD"):
        raise ReleaseError("public export only accepts the clean checked-out HEAD commit")
    entries = git_entries(root, ref)
    paths = [entry.path for entry in entries]
    policy_errors = policy_findings(paths, policy)
    policy_errors.extend(inventory_findings(paths, policy, required=True))
    if policy_errors:
        print_findings(policy_errors)
        raise ReleaseError("release policy does not classify every tracked file")

    staging = _safe_staging_path(output, root)
    output = output.resolve()
    staging.mkdir()
    try:
        for entry in entries:
            if not policy.allows(entry.path):
                continue
            if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
                raise ReleaseError(
                    f"unsupported Git object in public snapshot: {entry.path} "
                    f"({entry.mode} {entry.object_type})"
                )
            destination = staging / Path(entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_read_git_blob(root, entry.object_id))
            if entry.mode == "100755":
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        write_manifest(staging)
        findings = audit_tree(
            staging,
            policy,
            strict_policy=True,
            require_manifest=True,
        )
        if findings:
            print_findings(findings)
            raise ReleaseError("generated snapshot failed the public release audit")
        staging.replace(output)
    except BaseException:
        if staging.exists() and staging.parent == output.parent:
            shutil.rmtree(staging)
        raise
    print(f"public snapshot ready: {output}")
    print(
        "next step: create a new Git repository in that directory; never attach the source history"
    )
    return 0


def command_audit(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy_path = Path(args.policy).resolve() if args.policy else root / "public-release.toml"
    policy = load_policy(policy_path)
    if (root / ".git").exists():
        findings = audit_git_worktree(
            root,
            policy,
            strict_policy=args.strict_policy,
            require_manifest=args.require_manifest,
        )
    else:
        findings = audit_tree(
            root,
            policy,
            strict_policy=args.strict_policy,
            require_manifest=args.require_manifest,
        )
    if args.history:
        if not (root / ".git").exists():
            raise ReleaseError("--history requires a Git repository root")
        findings.extend(audit_history(root, policy))
    if findings:
        print_findings(findings)
        print(f"public release audit failed with {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("public release audit passed")
    return 0


def command_build(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy_path = Path(args.policy).resolve() if args.policy else root / "public-release.toml"
    if policy_path != root / "public-release.toml":
        raise ReleaseError("build policy must be the repository-root public-release.toml")
    policy = load_policy(policy_path)
    return build_snapshot(root, policy, Path(args.output), args.ref)


def command_inventory(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    ensure_git_root(root)
    if not git_is_clean(root):
        raise ReleaseError("inventory calculation requires a clean worktree and index")
    paths = [entry.path for entry in git_entries(root, "HEAD")]
    print(f"files={len(paths)}")
    print(f"inventory_sha256={path_inventory_sha256(paths)}")
    return 0


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit", help="audit a public tree or Git worktree")
    audit_parser.add_argument("--root", default=".", help="repository or snapshot root")
    audit_parser.add_argument("--policy", help="policy path; defaults below --root")
    audit_parser.add_argument(
        "--history",
        action="store_true",
        help="also scan every blob reachable from Git refs",
    )
    audit_parser.add_argument(
        "--strict-policy",
        action="store_true",
        help="require the approved exact file-inventory digest",
    )
    audit_parser.add_argument(
        "--require-manifest",
        action="store_true",
        help="require and verify PUBLIC_RELEASE_MANIFEST.json",
    )
    audit_parser.set_defaults(handler=command_audit)

    build_parser = subparsers.add_parser(
        "build",
        help="build an audited snapshot from a clean committed Git ref",
    )
    build_parser.add_argument("--root", default=".", help="source Git repository")
    build_parser.add_argument("--policy", help="policy path; defaults below --root")
    build_parser.add_argument("--ref", default="HEAD", help="committed Git ref to export")
    build_parser.add_argument("--output", required=True, help="new output directory outside source")
    build_parser.set_defaults(handler=command_build)

    inventory_parser = subparsers.add_parser(
        "inventory",
        help="print the exact tracked-path inventory digest for policy review",
    )
    inventory_parser.add_argument("--root", default=".", help="source Git repository")
    inventory_parser.set_defaults(handler=command_inventory)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ReleaseError, subprocess.SubprocessError) as exc:
        print(f"public release blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
