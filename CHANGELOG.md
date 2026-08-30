# Changelog

All notable changes to QuantSieve are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning while it remains pre-1.0.

## [0.1.1] - 2026-08-31

### Changed

- Made English the default README and moved the complete Simplified Chinese guide to `docs/README_ZH.md`.
- Reworked the project overview around the evidence chain, honest failure states, quick start, and contribution entry points.

### Added

- Added a repository social-preview image built from the recorded demo surface and an abstract evidence-sieve visual.
- Added verified CI, security-gate, release, license, and runtime badges to the default README.
- Added `CITATION.cff` with the repository's release metadata.

## [0.1.0] - 2026-08-30

### Added

- Grounded multi-market research, backtests, strategy discovery, factor diagnostics, portfolio experiments, delayed public-signal monitoring, and key-free recorded demos.
- Content-addressed data snapshots, server-owned run receipts, a deterministic event simulator, a Decimal risk kernel, and a simulation-only durable OMS.
- Docker Compose and local development paths, a standalone MCP server, bilingual documentation, security policy, threat model, and clean-history release tooling.

### Security

- Default Compose ports bind to localhost.
- The release gate audits the exported tree, reachable Git history, exact file inventory, secret-like content, workstation paths, and approved binary hashes.
- Provider work that outlives an HTTP deadline keeps its global capacity lease until the underlying task completes.

### Changed

- Selected QuantSieve as the public project name after a collision review of trading products and open-source repositories.
- Aligned quick-start documentation with tested repository-local installation paths and realistic cold-build expectations.
