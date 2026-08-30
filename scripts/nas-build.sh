#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  cat <<'EOF'
Usage: scripts/nas-build.sh <source-tar> <tag> <sha256>

Build clean QuantSieve API and Web images from a repository source archive.
Formal quantsieve-api:<tag> and quantsieve-web:<tag> tags are updated only
after both uniquely tagged candidate images pass every gate.
EOF
}

die() {
  echo "nas-build: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer"
}

require_credential_free_registry_url() {
  local name="$1"
  local value="$2"

  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] \
    || die "$name must be a single-line registry URL"
  [[ "$value" =~ ^https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]] \
    || die "$name must be a credential-free HTTP(S) registry URL"
}

if (( $# != 3 )); then
  usage >&2
  exit 2
fi

readonly source_tar_argument="$1"
readonly tag="$2"
readonly expected_sha256="${3,,}"

[[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] \
  || die "tag must contain 1-64 Docker-tag-safe characters"
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] \
  || die "sha256 must contain exactly 64 hexadecimal characters"

for required_command in \
  awk cp date docker find flock mkdir mktemp readlink rm sha256sum tar
do
  require_command "$required_command"
done

readonly source_tar="$(readlink -f -- "$source_tar_argument")"
[[ -f "$source_tar" ]] || die "source archive does not exist: $source_tar_argument"

readonly root_dir="${QUANTSIEVE_ROOT_DIR:-/srv/quantsieve}"
readonly build_root="${QUANTSIEVE_BUILD_ROOT:-$root_dir/build}"
readonly lock_file="${QUANTSIEVE_LOCK_FILE:-$root_dir/release.lock}"
readonly api_repository="${QUANTSIEVE_API_IMAGE_REPOSITORY:-quantsieve-api}"
readonly web_repository="${QUANTSIEVE_WEB_IMAGE_REPOSITORY:-quantsieve-web}"
readonly formal_api_image="${api_repository}:${tag}"
readonly formal_web_image="${web_repository}:${tag}"
readonly expected_api_user="${QUANTSIEVE_EXPECTED_API_USER:-quantsieve}"
readonly expected_api_workdir="${QUANTSIEVE_EXPECTED_API_WORKDIR:-/app}"
readonly expected_web_user="${QUANTSIEVE_EXPECTED_WEB_USER:-node}"
readonly expected_web_workdir="${QUANTSIEVE_EXPECTED_WEB_WORKDIR:-/app}"
readonly max_api_layers="${QUANTSIEVE_MAX_API_LAYERS:-12}"
readonly max_web_layers="${QUANTSIEVE_MAX_WEB_LAYERS:-20}"
readonly pull_base_images="${QUANTSIEVE_BUILD_PULL:-false}"
readonly build_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly build_id="${tag}-${build_timestamp}-$$"
readonly candidate_tag="${tag}-candidate-${build_timestamp}-$$"
readonly candidate_api_image="${api_repository}:${candidate_tag}"
readonly candidate_web_image="${web_repository}:${candidate_tag}"

require_positive_integer "QUANTSIEVE_MAX_API_LAYERS" "$max_api_layers"
require_positive_integer "QUANTSIEVE_MAX_WEB_LAYERS" "$max_web_layers"
[[ "$pull_base_images" =~ ^(true|false)$ ]] \
  || die "QUANTSIEVE_BUILD_PULL must be true or false"

[[ -d "$root_dir" ]] || die "root directory does not exist: $root_dir"
mkdir -p -- "$build_root"

exec 8>"$lock_file"
flock -n 8 || die "another QuantSieve build or deployment is already running"

if docker image inspect "$candidate_api_image" >/dev/null 2>&1; then
  die "candidate image already exists: $candidate_api_image"
fi
if docker image inspect "$candidate_web_image" >/dev/null 2>&1; then
  die "candidate image already exists: $candidate_web_image"
fi

workspace="$(mktemp -d "$build_root/${build_id}.XXXXXX")"
readonly workspace
readonly archive_copy="$workspace/source.tar"
readonly archive_members="$workspace/archive-members.txt"
readonly source_dir="$workspace/source"

api_candidate_created=0
web_candidate_created=0
api_formal_update_intent=0
web_formal_update_intent=0
build_committed=0
old_api_exists=0
old_web_exists=0
old_api_id=""
old_web_id=""
candidate_api_id=""
candidate_web_id=""

cleanup_workspace() {
  case "$workspace" in
    "$build_root"/"$build_id".*)
      rm -rf -- "$workspace"
      ;;
    *)
      echo "nas-build: refusing to remove unexpected path: $workspace" >&2
      ;;
  esac
}

image_id() {
  docker image inspect --format '{{.Id}}' "$1" 2>/dev/null
}

probed_image_state="unknown"
probed_image_id=""

probe_image_reference() {
  local image="$1"
  local current_id

  probed_image_state="unknown"
  probed_image_id=""
  if current_id="$(image_id "$image")"; then
    [[ "$current_id" =~ ^sha256:[[:xdigit:]]{64}$ ]] || return 1
    probed_image_state="present"
    probed_image_id="$current_id"
    return 0
  fi

  # `image inspect` returns non-zero both for a missing tag and for some daemon
  # failures. Only call the tag absent after an independent daemon probe.
  docker info >/dev/null 2>&1 || return 1
  probed_image_state="absent"
  return 0
}

restore_formal_tag() {
  local formal_image="$1"
  local candidate_id="$2"
  local old_exists="$3"
  local old_id="$4"

  probe_image_reference "$formal_image" || {
    echo "nas-build: could not determine current tag state: $formal_image" >&2
    return 1
  }

  if (( old_exists )) \
    && [[ "$probed_image_state" == "present" && "$probed_image_id" == "$old_id" ]]
  then
    return 0
  fi
  if (( ! old_exists )) && [[ "$probed_image_state" == "absent" ]]; then
    return 0
  fi

  [[ "$probed_image_state" == "present" \
    && "$probed_image_id" == "$candidate_id" ]] || {
    echo "nas-build: refusing to replace an externally changed tag: $formal_image" >&2
    return 1
  }

  if (( old_exists )); then
    # A Docker CLI failure can be ambiguous: the daemon may have changed the
    # tag before the client lost the response. Ignore the command status and
    # verify the resulting image ID independently below.
    docker image tag "$old_id" "$formal_image" >/dev/null 2>&1 || true
  else
    docker image rm "$formal_image" >/dev/null 2>&1 || true
  fi

  probe_image_reference "$formal_image" || {
    echo "nas-build: could not verify restored tag state: $formal_image" >&2
    return 1
  }
  if (( old_exists )); then
    [[ "$probed_image_state" == "present" && "$probed_image_id" == "$old_id" ]] || {
      echo "nas-build: failed to restore $formal_image to $old_id" >&2
      return 1
    }
  else
    [[ "$probed_image_state" == "absent" ]] || {
      echo "nas-build: failed to remove newly created tag $formal_image" >&2
      return 1
    }
  fi
}

cleanup_candidate_tags() {
  if (( web_candidate_created )); then
    if [[ "$(image_id "$candidate_web_image" || true)" == "$candidate_web_id" ]]; then
      docker image rm "$candidate_web_image" >/dev/null 2>&1 || true
    fi
  fi
  if (( api_candidate_created )); then
    if [[ "$(image_id "$candidate_api_image" || true)" == "$candidate_api_id" ]]; then
      docker image rm "$candidate_api_image" >/dev/null 2>&1 || true
    fi
  fi
}

on_exit() {
  local exit_code=$?
  local rollback_failed=0
  trap - EXIT INT TERM
  set +e

  if (( exit_code != 0 && ! build_committed )); then
    echo "nas-build: build failed; restoring any formal tags changed by this run" >&2
    if (( web_formal_update_intent )); then
      restore_formal_tag \
        "$formal_web_image" "$candidate_web_id" "$old_web_exists" "$old_web_id" \
        || rollback_failed=1
    fi
    if (( api_formal_update_intent )); then
      restore_formal_tag \
        "$formal_api_image" "$candidate_api_id" "$old_api_exists" "$old_api_id" \
        || rollback_failed=1
    fi
    cleanup_candidate_tags
    if (( rollback_failed )); then
      echo "nas-build: automatic tag rollback was incomplete; inspect formal tags manually" >&2
    fi
  fi

  cleanup_workspace
  exit "$exit_code"
}

trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -- "$source_dir"

assert_archive_member_safe() {
  local member="$1"
  local component
  local -a components

  [[ -n "$member" ]] || die "archive contains an empty member name"
  [[ "$member" != /* ]] || die "archive contains an absolute path: $member"
  [[ ! "$member" =~ ^[A-Za-z]:[/\\] ]] \
    || die "archive contains a drive-qualified path: $member"
  [[ "$member" != *\\* ]] \
    || die "archive contains a backslash path separator: $member"
  [[ ! "$member" =~ [[:cntrl:]] ]] \
    || die "archive contains a control character in a member name"

  IFS='/' read -r -a components <<< "$member"
  for component in "${components[@]}"; do
    [[ "$component" != ".." ]] \
      || die "archive contains a parent-directory traversal: $member"
  done
}

assert_image_metadata() {
  local image="$1"
  local maximum_layers="$2"
  local expected_user="$3"
  local expected_workdir="$4"
  local actual_layers
  local actual_user
  local actual_workdir

  actual_layers="$(docker image inspect --format '{{len .RootFS.Layers}}' "$image")"
  actual_user="$(docker image inspect --format '{{.Config.User}}' "$image")"
  actual_workdir="$(docker image inspect --format '{{.Config.WorkingDir}}' "$image")"

  [[ "$actual_layers" =~ ^[0-9]+$ ]] || die "invalid layer count for $image"
  (( actual_layers <= maximum_layers )) \
    || die "$image has $actual_layers layers; limit is $maximum_layers"
  [[ "$actual_user" == "$expected_user" ]] \
    || die "$image user is '$actual_user'; expected '$expected_user'"
  [[ "$actual_workdir" == "$expected_workdir" ]] \
    || die "$image workdir is '$actual_workdir'; expected '$expected_workdir'"
}

assert_image_history_credential_free() {
  local image="$1"
  local history

  history="$(
    docker history --no-trunc --format '{{.CreatedBy}}' "$image"
  )" || die "could not inspect complete image history for $image"
  if printf '%s\n' "$history" \
    | awk '{
        line = tolower($0)
        if (line ~ /https?:\/\/[^[:space:]\/]+@/) unsafe = 1
        if (line ~ /(token|password|passwd|secret|api[_-]?key)=[^[:space:]]+/) unsafe = 1
      }
      END { exit unsafe ? 0 : 1 }'
  then
    die "$image history contains credential-shaped build metadata"
  fi
}

assert_api_provenance_labels() {
  local image="$1"
  local expected_source_revision="$2"
  local expected_dependency_lock="$3"
  local source_revision
  local dependency_lock

  source_revision="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "$image"
  )" || die "could not read the API source revision label"
  dependency_lock="$(
    docker image inspect \
      --format '{{ index .Config.Labels "com.quantsieve.dependency-lock-sha256" }}' \
      "$image"
  )" || die "could not read the API dependency lock label"

  [[ "$source_revision" == "$expected_source_revision" ]] \
    || die "API image source revision label does not match the verified archive"
  [[ "$dependency_lock" == "$expected_dependency_lock" ]] \
    || die "API image dependency lock label does not match constraints.txt"
}

assert_web_provenance_label() {
  local image="$1"
  local expected_source_revision="$2"
  local source_revision

  source_revision="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "$image"
  )" || die "could not read the Web source revision label"

  [[ "$source_revision" == "$expected_source_revision" ]] \
    || die "Web image source revision label does not match the verified archive"
}

assert_formal_tag_unchanged() {
  local formal_image="$1"
  local old_exists="$2"
  local old_id="$3"
  local current_id

  if (( old_exists )); then
    current_id="$(image_id "$formal_image")" \
      || die "formal image disappeared during the build: $formal_image"
    [[ "$current_id" == "$old_id" ]] \
      || die "formal image changed during the build: $formal_image"
  elif docker image inspect "$formal_image" >/dev/null 2>&1; then
    die "formal image appeared during the build: $formal_image"
  fi
}

if old_api_id="$(image_id "$formal_api_image")"; then
  old_api_exists=1
fi
if old_web_id="$(image_id "$formal_web_image")"; then
  old_web_exists=1
fi

docker info >/dev/null
cp -- "$source_tar" "$archive_copy"
actual_sha256_line="$(sha256sum "$archive_copy")"
actual_sha256="${actual_sha256_line%%[[:space:]]*}"
[[ "$actual_sha256" == "$expected_sha256" ]] \
  || die "source archive SHA-256 mismatch: expected $expected_sha256, got $actual_sha256"

tar --list --file "$archive_copy" > "$archive_members"
while IFS= read -r archive_member || [[ -n "$archive_member" ]]; do
  assert_archive_member_safe "$archive_member"
done < "$archive_members"

# Repository archives do not require links or special files. Rejecting them
# closes symlink/hardlink traversal paths before extraction into the empty tree.
if tar --list --verbose --file "$archive_copy" \
  | awk 'substr($1, 1, 1) !~ /^[-d]$/ { bad = 1 } END { exit bad ? 0 : 1 }'
then
  die "archive may contain only regular files and directories"
fi

tar \
  --extract \
  --file "$archive_copy" \
  --directory "$source_dir" \
  --no-same-owner \
  --no-same-permissions

if [[ -n "$(find "$source_dir" -type l -print -quit)" ]]; then
  die "extracted source unexpectedly contains a symbolic link"
fi
[[ -f "$source_dir/apps/api/Dockerfile" ]] \
  || die "archive is missing apps/api/Dockerfile"
[[ -f "$source_dir/apps/api/constraints.txt" ]] \
  || die "archive is missing apps/api/constraints.txt"
[[ -f "$source_dir/apps/web/Dockerfile" ]] \
  || die "archive is missing apps/web/Dockerfile"

dependency_lock_sha256_line="$(
  sha256sum "$source_dir/apps/api/constraints.txt"
)"
readonly dependency_lock_sha256="${dependency_lock_sha256_line%%[[:space:]]*}"
[[ "$dependency_lock_sha256" =~ ^[0-9a-f]{64}$ ]] \
  || die "could not compute a valid dependency lock SHA-256"

api_build_args=()
web_build_args=()
pull_args=()

if [[ -n "${QUANTSIEVE_PIP_INDEX_URL:-}" ]]; then
  require_credential_free_registry_url \
    "QUANTSIEVE_PIP_INDEX_URL" "$QUANTSIEVE_PIP_INDEX_URL"
  api_build_args+=(--build-arg "PIP_INDEX_URL=$QUANTSIEVE_PIP_INDEX_URL")
fi
if [[ -n "${QUANTSIEVE_PYTHON_BASE_IMAGE:-}" ]]; then
  api_build_args+=(--build-arg "PYTHON_BASE_IMAGE=$QUANTSIEVE_PYTHON_BASE_IMAGE")
fi
if [[ -n "${QUANTSIEVE_NPM_REGISTRY:-}" ]]; then
  require_credential_free_registry_url \
    "QUANTSIEVE_NPM_REGISTRY" "$QUANTSIEVE_NPM_REGISTRY"
  web_build_args+=(--build-arg "NPM_CONFIG_REGISTRY=$QUANTSIEVE_NPM_REGISTRY")
fi
if [[ -n "${QUANTSIEVE_NODE_BASE_IMAGE:-}" ]]; then
  web_build_args+=(--build-arg "NODE_BASE_IMAGE=$QUANTSIEVE_NODE_BASE_IMAGE")
fi
if [[ "$pull_base_images" == "true" ]]; then
  pull_args+=(--pull)
fi

docker build \
  --no-cache \
  "${pull_args[@]}" \
  "${api_build_args[@]}" \
  --label "org.opencontainers.image.revision=$actual_sha256" \
  --label "com.quantsieve.dependency-lock-sha256=$dependency_lock_sha256" \
  --tag "$candidate_api_image" \
  --file "$source_dir/apps/api/Dockerfile" \
  "$source_dir"
api_candidate_created=1
candidate_api_id="$(image_id "$candidate_api_image")"
[[ "$candidate_api_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "could not resolve the candidate API image to an immutable ID"

docker build \
  --no-cache \
  "${pull_args[@]}" \
  "${web_build_args[@]}" \
  --label "org.opencontainers.image.revision=$actual_sha256" \
  --tag "$candidate_web_image" \
  --file "$source_dir/apps/web/Dockerfile" \
  "$source_dir"
web_candidate_created=1
candidate_web_id="$(image_id "$candidate_web_image")"
[[ "$candidate_web_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "could not resolve the candidate Web image to an immutable ID"

assert_image_metadata \
  "$candidate_api_id" \
  "$max_api_layers" \
  "$expected_api_user" \
  "$expected_api_workdir"
assert_image_metadata \
  "$candidate_web_id" \
  "$max_web_layers" \
  "$expected_web_user" \
  "$expected_web_workdir"
assert_image_history_credential_free "$candidate_api_id"
assert_image_history_credential_free "$candidate_web_id"
assert_api_provenance_labels \
  "$candidate_api_id" \
  "$actual_sha256" \
  "$dependency_lock_sha256"
assert_web_provenance_label "$candidate_web_id" "$actual_sha256"

docker run --rm --entrypoint python "$candidate_api_id" -c \
  'import quantsieve_api, quantsieve_engine, quantsieve_monitor, quantsieve_providers'
docker run --rm --entrypoint node "$candidate_web_id" --version

assert_formal_tag_unchanged "$formal_api_image" "$old_api_exists" "$old_api_id"
assert_formal_tag_unchanged "$formal_web_image" "$old_web_exists" "$old_web_id"

# Record intent before asking the daemon to mutate a formal tag. This covers
# both signals delivered between commands and an ambiguous CLI failure after
# the daemon has already committed the tag update.
api_formal_update_intent=1
docker image tag "$candidate_api_id" "$formal_api_image"
web_formal_update_intent=1
docker image tag "$candidate_web_id" "$formal_web_image"

[[ "$(image_id "$formal_api_image")" == "$candidate_api_id" ]] \
  || die "formal API tag does not reference the validated candidate"
[[ "$(image_id "$formal_web_image")" == "$candidate_web_id" ]] \
  || die "formal Web tag does not reference the validated candidate"

build_committed=1
cleanup_candidate_tags

echo "nas-build: build succeeded"
echo "  API: $formal_api_image ($candidate_api_id)"
echo "  Web: $formal_web_image ($candidate_web_id)"
