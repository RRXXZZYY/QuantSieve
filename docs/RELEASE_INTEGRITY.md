# Public release integrity

The development repository is treated as private even when most core source is
intended for open source. Its Git history may contain operational metadata that
is not part of the product.

## Required release path

1. Finish work on a clean, committed source revision.
2. Run the complete backend and frontend quality gates.
3. Generate a candidate outside the source repository:

   ```bash
   python scripts/public_release.py build --output ../quantsieve-public-candidate
   ```

4. Run the scanner again on the generated directory:

   ```bash
   python ../quantsieve-public-candidate/scripts/public_release.py \
     audit --root ../quantsieve-public-candidate \
     --strict-policy --require-manifest
   ```

5. Review the candidate file list and `PUBLIC_RELEASE_MANIFEST.json`.
6. Initialize a **new** Git repository in the candidate or synchronize it to a
   dedicated public mirror. Never copy `.git`, add a public remote to the
   development repository, merge its private history, or publish a workspace
   archive.
7. Before creating the fresh root commit, explicitly configure a repository-local
   public identity. Do not inherit a workstation's global Git identity:

   ```bash
   git config --local user.name "RRXXZZYY"
   git config --local user.email "RRXXZZYY@users.noreply.github.com"
   ```

8. Run CI against the public candidate before any release/tag is created.
9. A human explicitly approves the destination repository and release digest.
   The release tool intentionally has no push command.

The exporter reads Git blobs from a committed ref. It does not copy ignored
databases, `.env`, caches, dependencies, build output, untracked archives, or
workspace files.

## Gate behavior

The allowlist in [`public-release.toml`](../public-release.toml) is default
deny. Its exact file-inventory digest means a new tracked path blocks export
until the inventory is deliberately reviewed and resealed.

After reviewing a deliberate path addition/removal, calculate the replacement
digest with `python scripts/public_release.py inventory --root .` and update
`export.inventory_sha256` in the policy. The command is read-only and refuses a
dirty repository.
The audit also rejects:

- private keys and credential-like token values
- credentials embedded in connection URLs
- non-documentation IPv4/IPv6 addresses in file contents or path metadata
- workstation, mount, network-share and remote-shell paths
- private repository/planning references
- environment-specific secret files, databases, backups and key stores
- symlinks, submodules and unsupported Git objects

Findings report only the category and location, not the matched value.

The public GitHub workflow scans the entire reachable public history in addition
to the current tree, including path metadata, pathless reachable blobs, commit
messages, raw author and committer names and email addresses, annotated-tag
messages and tagger identity metadata, and reference names. Sensitive path
findings use an opaque path digest so the diagnostic cannot echo the value it
blocked. This is intentionally different from scanning the private source
history: the first public version must start from the generated snapshot as a
new root commit with the explicit public noreply identity configured above.

The same workflow verifies every file against `PUBLIC_RELEASE_MANIFEST.json`;
changing or deleting a public file without regenerating the manifest fails the
gate.

## GitHub repository settings

Immediately after creating the dedicated public repository, and before
announcing or tagging a release:

- enable private vulnerability reporting
- enable secret scanning and push protection; do not permit casual bypass
- protect `main` and require CI, security-gate and dependency-review checks
- require reviewed pull requests and dismiss approvals after new commits
- restrict force pushes, branch deletion and workflow changes
- use least-privilege workflow permissions
- publish releases from CI and add SBOM/provenance attestations when binary or
  container artifacts are distributed

GitHub controls are defense in depth. They do not make it safe to publish the
development repository or its old history.
