# NAS build and deployment

## Clean image build

`nas-build.sh` builds both images from a source tar only after verifying the
supplied SHA-256 and every archive path. It rejects absolute paths,
parent-directory components, links, and special files, then extracts into a new
temporary directory:

```bash
bash scripts/nas-build.sh <source-tar> <tag> <sha256>
```

Both Docker builds use the repository Dockerfiles and `--no-cache`. Images are
first written to invocation-unique candidate tags. The formal
`quantsieve-api:<tag>` and `quantsieve-web:<tag>` tags are changed only after
both candidates pass layer, user, working-directory, and runtime import gates.
If the two-tag update fails partway through, any changed formal tag is restored
to its original image ID. Rollback records tag-update intent before each Docker
call and independently probes the resulting image ID, so a lost client response
after a daemon-side tag update cannot leave an API/Web version split.

Both images record the verified source-archive SHA-256 as their OCI revision;
the API also records the exact `apps/api/constraints.txt` SHA-256 as an
QuantSieve dependency-lock label. The build reads all labels back before
promoting candidates, and deployment resolves immutable API/Web image IDs and
requires their source revisions to match.

The fault-injection regression test uses a fake Docker CLI that persists the
second tag update and then returns failure:

```bash
bash scripts/tests/nas-build-ambiguous-tag-rollback.sh
```

Optional non-secret build settings are passed as Docker build arguments:

```bash
export QUANTSIEVE_PIP_INDEX_URL='https://pypi.org/simple'
export QUANTSIEVE_NPM_REGISTRY='https://registry.npmjs.org'
export QUANTSIEVE_PYTHON_BASE_IMAGE='python:3.12-slim'
export QUANTSIEVE_NODE_BASE_IMAGE='node:24-alpine'
export QUANTSIEVE_BUILD_PULL=true
bash scripts/nas-build.sh ./quantsieve-0123abc.tar 0123abc '<sha256>'
```

Registry overrides must be credential-free HTTP(S) URLs without userinfo,
query strings, or fragments. The builder also scans complete candidate image
history for credential-shaped metadata before assigning formal tags. Use
registry login/configuration outside build arguments when authentication is
required.

Pin base images by digest for reproducible releases. The Dockerfile defaults
remain `python:3.12-slim` and `node:24-alpine`.

## Offline news translation sidecar

Run `nas-translation.sh` before deploying an API release with translation
enabled:

```bash
bash scripts/nas-translation.sh
export QUANTSIEVE_TRANSLATION_ENABLED=true
export QUANTSIEVE_TRANSLATION_BASE_URL=http://translate:5000
bash scripts/nas-deploy.sh <tag> <host-ip>
```

The first cold start downloads the pinned English-to-Chinese Argos package,
verifies its SHA-256, and installs it below
`${QUANTSIEVE_ROOT_DIR:-/srv/quantsieve}/translate-models`. The translator is limited to
3 GiB memory, 2 CPUs, and 256 PIDs; it joins only the QuantSieve Docker network
and publishes no host port. See [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)
for image, model, attribution, and license details.

Each update starts a uniquely named candidate without the stable `translate`
alias, waits for `/languages`, performs a real English-to-Chinese translation,
and probes it from the running API container. Only then does it move the stable
alias. Any switch failure removes only that invocation's candidate and restores
the previous container name, alias, and run state. A successful switch retains
the stopped previous container for manual rollback.

The current pre-sidecar QuantSieve installation may contain an unlabeled
container created during initial setup. Its one-time migration is deliberately
explicit:

```bash
QUANTSIEVE_TRANSLATION_ADOPT_LEGACY=true \
  bash scripts/nas-translation.sh
```

Legacy adoption succeeds only when the existing canonical container has no
foreign labels, uses the exact pinned image, has the expected model mount and
internal network alias, and exposes no host ports. Do not leave the adoption
flag enabled after migration.

Useful sidecar controls include:

- `QUANTSIEVE_ROOT_DIR`, `QUANTSIEVE_LOCK_FILE`, and
  `QUANTSIEVE_DOCKER_NETWORK`
- `QUANTSIEVE_TRANSLATION_MODEL_DIR` and
  `QUANTSIEVE_TRANSLATION_CONTAINER`
- `QUANTSIEVE_TRANSLATION_PROBE_CONTAINER`
- `QUANTSIEVE_TRANSLATION_IMAGE`, `QUANTSIEVE_TRANSLATION_MODEL_URL`, and
  `QUANTSIEVE_TRANSLATION_MODEL_SHA256`

Root, lock, and model paths are normalized before any permission change. The
model directory must be a non-symlink descendant of the QuantSieve root.

## Guarded deployment

`nas-deploy.sh` performs a guarded update of an existing QuantSieve deployment.
Run it on the NAS only after both release images have been built:

```bash
bash scripts/nas-deploy.sh <tag> <host-ip>
```

The host IP is deliberately required on every invocation; the repository does
not contain a production address. The script expects
`quantsieve-api:<tag>` and `quantsieve-web:<tag>`, the `quantsieve` Docker
network, and the existing `quantsieve-api` and `quantsieve-web` containers.

Before stopping production it:

- takes a consistent SQLite `.backup`, validates it, and atomically publishes it;
- rejects API images over 12 layers or Web images over 20 layers;
- verifies image users, working directories, and Python package imports;
- resolves both tags once to lowercase immutable image IDs, validates the API
  provenance labels from that ID, and injects them into run manifests;
- starts isolated candidate containers against a copy of the database;
- verifies API and proxied Web health responses report the requested version;
- verifies the factor catalog, Paper OMS schema, SQLite integrity, and
  foreign-key consistency for both the candidate and promoted production API;
- atomically records the successful tag as the single line in
  `$QUANTSIEVE_ROOT_DIR/DEPLOYED_VERSION`, owned like the previous receipt (or
  the root directory when first created) and always mode `0600`.

Deployment fault-injection regressions verify that a post-start contract
failure reaches guarded rollback, that rollback re-probes every possible API
writer immediately before replacing SQLite, and that image provenance is
required:

```bash
bash scripts/tests/nas-deploy-contract-rollback.sh
bash scripts/tests/nas-deploy-final-writer-probe.sh
bash scripts/tests/nas-deploy-provenance.sh
bash scripts/tests/nas-deploy-version-receipt.sh
```

Builds and deployments share a `flock` lock. On failure, rollback only removes
new containers created by that invocation. On success, the previous stopped
containers and database backup are retained, and their names are printed.

`DEPLOYED_VERSION` is snapshotted without requiring it to match the currently
running container, so an older operational receipt does not block recovery.
Publication rechecks the original inode, metadata, and bytes immediately before
a same-directory atomic rename. Any later transaction failure restores the
previous bytes, or removes the new receipt when none existed. Rollback restores
the receipt only while the path still names this invocation's verified inode.
External changes detected before publication or after it are not overwritten,
and previous services remain stopped for manual recovery when that ownership
proof is lost. Official deployment writers must share the release lock.

If a new production API has started, a later failure is treated as a possible
schema/data change. The rollback first stops the new containers, preserves a
consistent failed-state forensic backup, removes only the exact SQLite
`-wal`/`-shm` sidecars, and atomically restores the validated pre-deployment
backup with its original owner and mode. Previous services are restarted only
after that restore succeeds; otherwise they remain stopped and the script
prints the manual recovery paths.

Rollback fails closed on Docker query errors: it replaces SQLite only after
independent probes explicitly prove every API that could write it is absent or
has `State.Running=false`. Container renames are recovered by the original
container ID, image ID, and recorded run state rather than by a rename command's
exit code alone.

After a deployment has completed successfully, starting an older API image by
itself is not a schema rollback. To downgrade, first stop and independently
verify the absence of every API process that can write the production database,
preserve a forensic copy, atomically restore the matching pre-deployment
database backup (removing only its exact `-wal`/`-shm` sidecars), and only then
restart the retained older containers. If any writer-stop or restore check
fails, keep the services stopped and recover manually.

The durable Paper OMS kill switch starts engaged and is never cleared by a
deployment or restart. Inspect it only inside the API container:

```bash
docker exec quantsieve-api sh -lc \
  'python -m quantsieve_api.paper_oms_operator \
    --database "$QUANTSIEVE_DATABASE_PATH" status'
```

An operator can engage or clear it only with the revision returned by `status`.
Clearing additionally requires the exact simulation-only confirmation phrase:

```bash
docker exec quantsieve-api sh -lc \
  'python -m quantsieve_api.paper_oms_operator \
    --database "$QUANTSIEVE_DATABASE_PATH" clear \
    --expected-revision 1 \
    --confirm "CLEAR PAPER OMS KILL SWITCH - SIMULATION ONLY"'
```

The CLI has no HTTP route, refuses to initialize a missing authority, uses an
atomic revision comparison, and emits a sanitized fail-closed error. Do not
clear the switch merely to make a deployment smoke test pass.

Configuration is supplied through environment variables, so the script contains
no API keys. Common examples:

```bash
export QUANTSIEVE_SEC_USER_AGENT='QuantSieve operator contact@example.com'
export QUANTSIEVE_LLM_API_KEY='...'
export QUANTSIEVE_X_BEARER_TOKEN='...'
bash scripts/nas-deploy.sh 0123abc <private-host-ip>
```

Useful deployment controls include:

- `QUANTSIEVE_ROOT_DIR` and `QUANTSIEVE_DATA_DIR`
- `QUANTSIEVE_DATABASE_PATH` (the in-container `/app/data/...` path)
- `QUANTSIEVE_LOCK_FILE`
- `QUANTSIEVE_DOCKER_NETWORK` and `QUANTSIEVE_WEB_PORT`
- `QUANTSIEVE_MAX_API_LAYERS` and `QUANTSIEVE_MAX_WEB_LAYERS`
- `QUANTSIEVE_EXPECTED_API_USER` and `QUANTSIEVE_EXPECTED_API_WORKDIR`
- `QUANTSIEVE_HEALTH_ATTEMPTS` and `QUANTSIEVE_HEALTH_INTERVAL_SECONDS`
- `QUANTSIEVE_FACTOR_RESEARCH_MAX_CONCURRENCY`,
  `QUANTSIEVE_FACTOR_RESEARCH_PROVIDER_MAX_CONCURRENCY`, and
  `QUANTSIEVE_FACTOR_RESEARCH_DEADLINE_SECONDS`

`QUANTSIEVE_DATABASE_PATH` is never a host path. Its path relative to
`/app/data` is used under `QUANTSIEVE_DATA_DIR` for the production database and
under the isolated staging mount for the candidate database, so backup, test,
and production always address the same logical file. The deployer also inherits
the existing API's strictly whitelisted LLM, X, SEC, translation, factor
research resource-limit, and scheduler settings when the deployment shell does
not explicitly provide them; secret values are not placed in Docker command
arguments or printed. When translation is enabled, both the candidate and
production API must pass a direct sidecar translation plus a stored-event
translation-route smoke test before deployment can complete.

The normal API working directory is `/app`. If a locally built compatibility
image uses another working directory, set `QUANTSIEVE_EXPECTED_API_WORKDIR`
explicitly for that deployment rather than recording an operational image tag
in the repository.

After an observation period, old stopped containers may be removed manually.
They are deliberately never deleted inside the deployment transaction.
