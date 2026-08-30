# Security policy

## Supported versions

QuantSieve is pre-1.0 software. Security fixes are applied to the current
`main` branch and the latest tagged release. Older snapshots are not supported.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability and do not include
credentials, private market data, account details, positions, or server
information in a reproduction.

After the public repository is created, use GitHub's **Report a
vulnerability** private security-advisory flow. Until then, report the issue
directly to the maintainer through an already established private channel.
Include:

- the affected version and component
- minimal reproduction steps or a proof of concept
- expected impact
- any known workaround or suggested mitigation

The maintainer will acknowledge a complete report, validate it privately, and
coordinate disclosure after a fix is available.

## Deployment boundary

The default Docker Compose configuration is for a single trusted operator on a
private network. Before exposing QuantSieve beyond localhost:

- place it behind authenticated TLS termination
- set an explicit CORS allowlist
- keep databases, caches, model files, and `.env` outside the web root
- inject secrets at runtime; never bake them into source, images, or command
  arguments
- disable custom strategy execution for untrusted users
- restrict outbound network access to required data and model providers
- back up and test restoration of persistent data

Browser-supplied LLM keys are stored in browser local storage and sent with
individual requests. That is a convenience for a trusted single-user browser,
not a shared-workstation or multi-tenant secret vault.

The strategy subprocess uses syntax restrictions, a separate process, timeouts,
and platform resource limits to reduce accidental local harm. It is **not** a
security boundary for hostile code or multi-tenant workloads.

Paper-trading features do not authorize or submit real orders. Real broker or
exchange credentials are outside the public edition's trust boundary.

## Public release controls

The development repository is not published directly. A public release is
generated as a new-history snapshot from the reviewed allowlist in
[`public-release.toml`](public-release.toml), then checked by
[`scripts/public_release.py`](scripts/public_release.py).

The release gate rejects unclassified file inventories, stale manifests,
secret-like values, private keys,
non-documentation network addresses, workstation/server paths, databases,
caches, and private-plan references. GitHub push protection, dependency review,
branch protection, and the repository CI are additional controls; they do not
replace the local release gate.

If a real credential is ever exposed, revoke or rotate it first. Deleting the
current file is not sufficient because Git history may retain the value.

The trust boundaries and release procedure are documented in
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) and
[`docs/RELEASE_INTEGRITY.md`](docs/RELEASE_INTEGRITY.md).

## Known limitations

- Public data providers can be delayed, incomplete, revised, or unavailable.
- The default deployment has no multi-user identity, authorization, or tenant
  isolation.
- QuantSieve is research software, not a custody system and not investment
  advice.
