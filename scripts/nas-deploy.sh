#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  cat <<'EOF'
Usage: scripts/nas-deploy.sh <tag> <host-ip>

Deploy prebuilt quantsieve-api:<tag> and quantsieve-web:<tag> images on the NAS.
The deployment address is required explicitly; no production host is embedded.
EOF
}

die() {
  echo "nas-deploy: $*" >&2
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

require_ipv4() {
  local address="$1"
  local octet
  local -a octets

  IFS='.' read -r -a octets <<< "$address"
  (( ${#octets[@]} == 4 )) || die "host-ip must contain four IPv4 octets"
  for octet in "${octets[@]}"; do
    (( 10#$octet <= 255 )) || die "host-ip contains an octet over 255"
  done
}

require_container_data_path() {
  local path="$1"
  local component
  local relative_path
  local -a components

  [[ "$path" == /app/data/* ]] \
    || die "QUANTSIEVE_DATABASE_PATH must be inside /app/data"
  relative_path="${path#/app/data/}"
  [[ -n "$relative_path" ]] \
    || die "QUANTSIEVE_DATABASE_PATH must name a database file"
  [[ "$relative_path" =~ ^[A-Za-z0-9._/-]+$ ]] \
    || die "QUANTSIEVE_DATABASE_PATH contains an unsafe character"
  IFS='/' read -r -a components <<< "$relative_path"
  for component in "${components[@]}"; do
    [[ -n "$component" && "$component" != "." && "$component" != ".." ]] \
      || die "QUANTSIEVE_DATABASE_PATH contains an unsafe path component"
  done
}

if (( $# != 2 )); then
  usage >&2
  exit 2
fi

readonly tag="$1"
readonly host_ip="$2"

[[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] \
  || die "tag must contain 1-64 Docker-tag-safe characters"
[[ "$host_ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || die "host-ip must be an IPv4 address"
require_ipv4 "$host_ip"

readonly root_dir="${QUANTSIEVE_ROOT_DIR:-/srv/quantsieve}"
readonly data_dir="${QUANTSIEVE_DATA_DIR:-$root_dir/data}"
readonly container_database_path="${QUANTSIEVE_DATABASE_PATH:-/app/data/quantsieve.db}"
require_container_data_path "$container_database_path"
readonly database_relative_path="${container_database_path#/app/data/}"
readonly database_path="$data_dir/$database_relative_path"
readonly database_dir="$(dirname -- "$database_path")"
readonly cache_path="${QUANTSIEVE_CACHE_PATH:-/app/data/cache.db}"
readonly backup_dir="$data_dir/backups"
readonly staging_root="$root_dir/staging"
readonly lock_file="${QUANTSIEVE_LOCK_FILE:-$root_dir/release.lock}"
readonly network="${QUANTSIEVE_DOCKER_NETWORK:-quantsieve}"
readonly web_port="${QUANTSIEVE_WEB_PORT:-8100}"
readonly api_repository="${QUANTSIEVE_API_IMAGE_REPOSITORY:-quantsieve-api}"
readonly web_repository="${QUANTSIEVE_WEB_IMAGE_REPOSITORY:-quantsieve-web}"
readonly api_image="${api_repository}:${tag}"
readonly web_image="${web_repository}:${tag}"
readonly expected_api_user="${QUANTSIEVE_EXPECTED_API_USER:-quantsieve}"
readonly expected_api_workdir="${QUANTSIEVE_EXPECTED_API_WORKDIR:-/app}"
readonly expected_web_user="${QUANTSIEVE_EXPECTED_WEB_USER:-node}"
readonly expected_web_workdir="${QUANTSIEVE_EXPECTED_WEB_WORKDIR:-/app}"
readonly max_api_layers="${QUANTSIEVE_MAX_API_LAYERS:-12}"
readonly max_web_layers="${QUANTSIEVE_MAX_WEB_LAYERS:-20}"
readonly health_attempts="${QUANTSIEVE_HEALTH_ATTEMPTS:-30}"
readonly health_interval_seconds="${QUANTSIEVE_HEALTH_INTERVAL_SECONDS:-1}"
readonly cors_origins="${QUANTSIEVE_CORS_ORIGINS:-http://$host_ip:$web_port}"
readonly deployment_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly deployment_id="${tag}-${deployment_timestamp}-$$"
readonly old_api_name="quantsieve-api-previous-${deployment_id}"
readonly old_web_name="quantsieve-web-previous-${deployment_id}"
readonly candidate_api_name="quantsieve-api-candidate-${deployment_id}"
readonly candidate_web_name="quantsieve-web-candidate-${deployment_id}"
readonly backup_path="$backup_dir/quantsieve-pre-${deployment_id}.db"
readonly forensic_path="$backup_dir/quantsieve-failed-${deployment_id}.db"
readonly deployed_version_path="$root_dir/DEPLOYED_VERSION"

require_positive_integer "QUANTSIEVE_WEB_PORT" "$web_port"
require_positive_integer "QUANTSIEVE_MAX_API_LAYERS" "$max_api_layers"
require_positive_integer "QUANTSIEVE_MAX_WEB_LAYERS" "$max_web_layers"
require_positive_integer "QUANTSIEVE_HEALTH_ATTEMPTS" "$health_attempts"
require_positive_integer \
  "QUANTSIEVE_HEALTH_INTERVAL_SECONDS" \
  "$health_interval_seconds"
(( web_port <= 65535 )) || die "QUANTSIEVE_WEB_PORT must be at most 65535"

for required_command in \
  base64 chown chmod cmp cp curl date dirname docker flock mkdir mktemp mv rm \
  rmdir stat sqlite3 tr
do
  require_command "$required_command"
done

[[ -d "$root_dir" ]] || die "root directory does not exist: $root_dir"
[[ -d "$data_dir" ]] || die "data directory does not exist: $data_dir"
[[ "$data_dir" == /* ]] || die "data directory must be an absolute host path"
[[ -f "$database_path" ]] || die "database does not exist: $database_path"
[[ ! -L "$database_path" ]] || die "database path must not be a symbolic link"
mkdir -p -- "$backup_dir" "$staging_root"

exec 9>"$lock_file"
flock -n 9 || die "another QuantSieve deployment is already running"

old_api_renamed=0
old_web_renamed=0
old_api_rename_intent=0
old_web_rename_intent=0
old_api_restored_to_canonical=0
old_web_restored_to_canonical=0
new_api_created=0
new_web_created=0
new_api_container_id=""
new_web_container_id=""
new_api_image_id=""
new_web_image_id=""
api_source_revision=""
api_dependency_lock_sha256=""
web_source_revision=""
production_database_exposed=0
candidate_api_created=0
candidate_web_created=0
backup_temp_dir=""
forensic_temp_dir=""
restore_temp_path=""
staging_dir=""
deployed_version_temp_dir=""
deployed_version_snapshot_path=""
deployed_version_expected_path=""
deployed_version_snapshot_state="unprepared"
deployed_version_snapshot_fingerprint=""
deployed_version_uid=""
deployed_version_gid=""
deployed_version_written=0
deployed_version_written_identity=""
deployed_version_conflict=0
database_uid=""
database_gid=""
database_mode=""
original_api_container_id=""
original_api_image_id=""
original_api_was_running=""
original_web_container_id=""
original_web_image_id=""
original_web_was_running=""
probed_container_id=""
probed_container_image_id=""
probed_container_name=""
probed_container_state="unknown"
quiesced_container_state="unknown"
recorded_created_container_id=""

deployed_version_fingerprint() {
  local path="$1"

  [[ -f "$path" && ! -L "$path" ]] || return 1
  stat -c '%d:%i:%u:%g:%a:%s:%Y:%Z' -- "$path"
}

deployed_version_identity() {
  local path="$1"

  [[ -f "$path" && ! -L "$path" ]] || return 1
  stat -c '%d:%i' -- "$path"
}

snapshot_deployed_version() {
  local initial_fingerprint
  local final_fingerprint

  [[ "$deployed_version_snapshot_state" == "unprepared" ]] || return 1
  deployed_version_temp_dir="$(
    mktemp -d "$root_dir/.deployed-version-${deployment_id}.XXXXXX"
  )" || return 1
  chmod 700 "$deployed_version_temp_dir" || return 1
  deployed_version_snapshot_path="$deployed_version_temp_dir/original"
  deployed_version_expected_path="$deployed_version_temp_dir/expected"

  if [[ -L "$deployed_version_path" ]]; then
    echo "nas-deploy: DEPLOYED_VERSION must not be a symbolic link" >&2
    return 1
  fi
  if [[ -e "$deployed_version_path" ]]; then
    [[ -f "$deployed_version_path" ]] || {
      echo "nas-deploy: DEPLOYED_VERSION must be a regular file" >&2
      return 1
    }
    initial_fingerprint="$(
      deployed_version_fingerprint "$deployed_version_path"
    )" || return 1
    cp --preserve=mode,ownership,timestamps -- \
      "$deployed_version_path" "$deployed_version_snapshot_path" || return 1
    chmod 600 "$deployed_version_snapshot_path" || return 1
    final_fingerprint="$(
      deployed_version_fingerprint "$deployed_version_path"
    )" || return 1
    if [[ "$initial_fingerprint" != "$final_fingerprint" ]] \
      || ! cmp -s -- \
        "$deployed_version_path" "$deployed_version_snapshot_path"
    then
      deployed_version_conflict=1
      echo "nas-deploy: DEPLOYED_VERSION changed while it was being recorded" >&2
      return 1
    fi
    deployed_version_snapshot_fingerprint="$initial_fingerprint"
    deployed_version_uid="$(stat -c '%u' -- "$deployed_version_path")" \
      || return 1
    deployed_version_gid="$(stat -c '%g' -- "$deployed_version_path")" \
      || return 1
    deployed_version_snapshot_state="present"
  else
    deployed_version_uid="$(stat -c '%u' -- "$root_dir")" || return 1
    deployed_version_gid="$(stat -c '%g' -- "$root_dir")" || return 1
    deployed_version_snapshot_state="absent"
  fi

  [[ "$deployed_version_uid" =~ ^[0-9]+$ \
    && "$deployed_version_gid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$tag" >"$deployed_version_expected_path" || return 1
  chown "$deployed_version_uid:$deployed_version_gid" \
    "$deployed_version_expected_path" || return 1
  chmod 600 "$deployed_version_expected_path" || return 1
}

deployed_version_matches_snapshot() {
  local current_fingerprint

  case "$deployed_version_snapshot_state" in
    absent)
      [[ ! -e "$deployed_version_path" && ! -L "$deployed_version_path" ]]
      ;;
    present)
      current_fingerprint="$(
        deployed_version_fingerprint "$deployed_version_path"
      )" || return 1
      [[ "$current_fingerprint" == "$deployed_version_snapshot_fingerprint" ]] \
        || return 1
      cmp -s -- "$deployed_version_path" "$deployed_version_snapshot_path"
      ;;
    *)
      return 1
      ;;
  esac
}

verify_written_deployed_version() {
  local current_identity

  (( deployed_version_written )) || return 1
  current_identity="$(
    deployed_version_identity "$deployed_version_path"
  )" || return 1
  [[ "$current_identity" == "$deployed_version_written_identity" ]] \
    || return 1
  [[ "$(stat -c '%u' -- "$deployed_version_path")" \
    == "$deployed_version_uid" ]] || return 1
  [[ "$(stat -c '%g' -- "$deployed_version_path")" \
    == "$deployed_version_gid" ]] || return 1
  [[ "$(stat -c '%a' -- "$deployed_version_path")" == "600" ]] || return 1
  cmp -s -- "$deployed_version_path" "$deployed_version_expected_path"
}

publish_deployed_version() {
  local next_path="$deployed_version_temp_dir/next"
  local next_identity
  local publish_status=0
  local current_identity

  if ! deployed_version_matches_snapshot; then
    deployed_version_conflict=1
    echo "nas-deploy: DEPLOYED_VERSION changed during deployment" >&2
    return 1
  fi

  cp --preserve=mode,ownership,timestamps -- \
    "$deployed_version_expected_path" "$next_path" || return 1
  chown "$deployed_version_uid:$deployed_version_gid" "$next_path" || return 1
  chmod 600 "$next_path" || return 1
  next_identity="$(deployed_version_identity "$next_path")" || return 1

  # The rename is atomic because the temporary file is in the same directory.
  # Record the destination by inode even when mv reports an ambiguous failure.
  mv -f -- "$next_path" "$deployed_version_path" || publish_status=$?
  current_identity="$(
    deployed_version_identity "$deployed_version_path"
  )" || current_identity=""
  if [[ "$current_identity" == "$next_identity" ]]; then
    deployed_version_written=1
    deployed_version_written_identity="$next_identity"
  else
    deployed_version_conflict=1
    echo "nas-deploy: could not establish DEPLOYED_VERSION ownership" >&2
    return 1
  fi

  (( publish_status == 0 )) || return "$publish_status"
  if ! verify_written_deployed_version; then
    deployed_version_conflict=1
    echo "nas-deploy: DEPLOYED_VERSION failed post-write verification" >&2
    return 1
  fi
}

restore_deployed_version() {
  local restore_path="$deployed_version_temp_dir/restore"

  if [[ "$deployed_version_snapshot_state" == "unprepared" ]]; then
    return 0
  fi
  if (( ! deployed_version_written )); then
    if (( deployed_version_conflict )) \
      || ! deployed_version_matches_snapshot
    then
      echo \
        "nas-deploy: DEPLOYED_VERSION changed externally; refusing to overwrite it" \
        >&2
      return 1
    fi
    return 0
  fi

  # Restore only while the pathname still names the exact inode published by
  # this invocation. An external replacement or in-place edit is never erased.
  if ! verify_written_deployed_version; then
    echo \
      "nas-deploy: DEPLOYED_VERSION changed externally; refusing to overwrite it" \
      >&2
    return 1
  fi

  case "$deployed_version_snapshot_state" in
    present)
      cp --preserve=mode,ownership,timestamps -- \
        "$deployed_version_snapshot_path" "$restore_path" || return 1
      chown "$deployed_version_uid:$deployed_version_gid" "$restore_path" \
        || return 1
      # Re-establish the ownership proof at the final replacement boundary.
      verify_written_deployed_version || return 1
      mv -f -- "$restore_path" "$deployed_version_path" || return 1
      [[ -f "$deployed_version_path" && ! -L "$deployed_version_path" ]] \
        || return 1
      [[ "$(stat -c '%u' -- "$deployed_version_path")" \
        == "$deployed_version_uid" ]] || return 1
      [[ "$(stat -c '%g' -- "$deployed_version_path")" \
        == "$deployed_version_gid" ]] || return 1
      cmp -s -- \
        "$deployed_version_path" "$deployed_version_snapshot_path" || return 1
      ;;
    absent)
      verify_written_deployed_version || return 1
      rm -f -- "$deployed_version_path" || return 1
      [[ ! -e "$deployed_version_path" && ! -L "$deployed_version_path" ]] \
        || return 1
      ;;
    *)
      return 1
      ;;
  esac
  deployed_version_written=0
  deployed_version_written_identity=""
  return 0
}

container_matches_deployment() {
  local container_name="$1"
  local expected_role="$2"
  local actual_deployment
  local actual_role

  actual_deployment="$(
    docker container inspect \
      --format '{{index .Config.Labels "com.quantsieve.deployment"}}' \
      "$container_name" 2>/dev/null
  )" || return 1
  actual_role="$(
    docker container inspect \
      --format '{{index .Config.Labels "com.quantsieve.role"}}' \
      "$container_name" 2>/dev/null
  )" || return 1
  [[ "$actual_deployment" == "$deployment_id" && "$actual_role" == "$expected_role" ]]
}

probe_container_by_id() {
  local expected_id="$1"
  local metadata
  local listed_ids
  local container_id
  local image_id
  local container_name
  local running
  local extra

  probed_container_id=""
  probed_container_image_id=""
  probed_container_name=""
  probed_container_state="unknown"

  if metadata="$(
    docker container inspect \
      --format '{{.Id}}|{{.Image}}|{{.Name}}|{{.State.Running}}' \
      "$expected_id" 2>/dev/null
  )"
  then
    [[ -n "$metadata" && "$metadata" != *$'\n'* ]] || return 1
    IFS='|' read -r \
      container_id image_id container_name running extra <<< "$metadata"
    [[ "$container_id" == "$expected_id" ]] || return 1
    [[ "$container_id" =~ ^[[:xdigit:]]{64}$ ]] || return 1
    [[ "$image_id" =~ ^sha256:[[:xdigit:]]{64}$ ]] || return 1
    [[ "$container_name" == /* && -z "$extra" ]] || return 1
    case "$running" in
      true)
        probed_container_state="running"
        ;;
      false)
        probed_container_state="stopped"
        ;;
      *)
        return 1
        ;;
    esac
    probed_container_id="$container_id"
    probed_container_image_id="$image_id"
    probed_container_name="$container_name"
    return 0
  fi

  # An inspect error is not proof of absence. Only a successful, empty
  # all-container listing establishes that this exact immutable ID is absent.
  if ! listed_ids="$(
    docker container ls --all --no-trunc \
      --filter "id=$expected_id" --format '{{.ID}}' 2>/dev/null
  )"
  then
    return 1
  fi
  if [[ -z "$listed_ids" ]]; then
    probed_container_state="absent"
    return 0
  fi

  # The daemon says the ID exists but could not provide trustworthy metadata.
  return 1
}

probe_container_by_name() {
  local expected_name="$1"
  local metadata
  local listed_containers
  local container_id
  local image_id
  local container_name
  local running
  local extra

  probed_container_id=""
  probed_container_image_id=""
  probed_container_name=""
  probed_container_state="unknown"

  if metadata="$(
    docker container inspect \
      --format '{{.Id}}|{{.Image}}|{{.Name}}|{{.State.Running}}' \
      "$expected_name" 2>/dev/null
  )"
  then
    [[ -n "$metadata" && "$metadata" != *$'\n'* ]] || return 1
    IFS='|' read -r \
      container_id image_id container_name running extra <<< "$metadata"
    [[ "$container_id" =~ ^[[:xdigit:]]{64}$ ]] || return 1
    [[ "$image_id" =~ ^sha256:[[:xdigit:]]{64}$ ]] || return 1
    [[ "$container_name" == "/$expected_name" && -z "$extra" ]] || return 1
    case "$running" in
      true)
        probed_container_state="running"
        ;;
      false)
        probed_container_state="stopped"
        ;;
      *)
        return 1
        ;;
    esac
    probed_container_id="$container_id"
    probed_container_image_id="$image_id"
    probed_container_name="$container_name"
    return 0
  fi

  # As above, a failed inspect remains unknown unless a separate successful
  # exact-name listing proves that no such container exists.
  if ! listed_containers="$(
    docker container ls --all --no-trunc \
      --filter "name=^/${expected_name}$" \
      --format '{{.ID}}|{{.Names}}' 2>/dev/null
  )"
  then
    return 1
  fi
  if [[ -z "$listed_containers" ]]; then
    probed_container_state="absent"
    return 0
  fi
  return 1
}

inherit_api_environment() {
  local container_id="$1"
  local environment_name
  local environment_value
  local encoded_value
  local read_status

  for environment_name in "${api_environment_whitelist[@]}"; do
    # A value supplied to this deployment shell always wins. Export it so
    # Docker's value-less --env NAME form can forward it without placing the
    # secret value in the Docker CLI arguments.
    if [[ -v "$environment_name" ]]; then
      environment_value="${!environment_name}"
    else
      # Query one whitelisted name at a time and base64-wrap stdout. This never
      # enumerates unrelated container variables and never writes a secret to
      # the terminal or Docker command line.
      if encoded_value="$(
        docker exec "$container_id" python -c \
          'import base64, os, sys
key = sys.argv[1].encode("ascii")
if key not in os.environb:
    raise SystemExit(3)
value = os.environb[key]
if b"\n" in value or b"\r" in value:
    raise SystemExit(4)
sys.stdout.write(base64.b64encode(value).decode("ascii"))' \
          "$environment_name" 2>/dev/null
      )"
      then
        environment_value="$(
          printf '%s' "$encoded_value" | base64 -d 2>/dev/null
        )" || die "could not decode previous API value: $environment_name"
      else
        read_status=$?
        if (( read_status == 3 )); then
          continue
        fi
        die "could not read previous API value: $environment_name"
      fi
      if [[ -z "$encoded_value" && -n "$environment_value" ]]; then
        die "malformed previous API value: $environment_name"
      fi
      if [[ -n "$encoded_value" && ! "$encoded_value" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]; then
        die "malformed previous API encoding: $environment_name"
      fi
      if (( ${#encoded_value} % 4 != 0 )); then
        die "malformed previous API encoding length: $environment_name"
      fi
      printf -v "$environment_name" '%s' "$environment_value"
    fi

    [[ "$environment_value" != *$'\n'* \
      && "$environment_value" != *$'\r'* ]] \
      || die "multiline API environment value is not supported: $environment_name"
    export "$environment_name"
  done
}

record_created_container_identity() {
  local container_name="$1"
  local expected_image_id="$2"
  local expected_role="$3"

  recorded_created_container_id=""
  probe_container_by_name "$container_name" || return 1
  [[ "$probed_container_state" == "stopped" ]] || return 1
  [[ "$probed_container_image_id" == "$expected_image_id" ]] || return 1
  container_matches_deployment "$probed_container_id" "$expected_role" || return 1
  recorded_created_container_id="$probed_container_id"
}

cleanup_candidate_containers() {
  local best_effort="${1:-false}"
  local cleanup_failed=0

  if (( candidate_web_created )); then
    if container_matches_deployment "$candidate_web_name" "web-candidate"; then
      if docker rm -f "$candidate_web_name" >/dev/null 2>&1; then
        candidate_web_created=0
      else
        cleanup_failed=1
      fi
    else
      echo "nas-deploy: refusing to remove an unowned candidate Web container" >&2
      cleanup_failed=1
    fi
  fi
  if (( candidate_api_created )); then
    if container_matches_deployment "$candidate_api_name" "api-candidate"; then
      if docker rm -f "$candidate_api_name" >/dev/null 2>&1; then
        candidate_api_created=0
      else
        cleanup_failed=1
      fi
    else
      echo "nas-deploy: refusing to remove an unowned candidate API container" >&2
      cleanup_failed=1
    fi
  fi

  if (( cleanup_failed )) && [[ "$best_effort" != "true" ]]; then
    return 1
  fi
  return 0
}

cleanup_generated_directories() {
  if [[ -n "$deployed_version_temp_dir" ]]; then
    case "$deployed_version_temp_dir" in
      "$root_dir"/.deployed-version-"$deployment_id".*)
        rm -rf -- "$deployed_version_temp_dir"
        ;;
      *)
        echo \
          "nas-deploy: refusing to remove unexpected path: $deployed_version_temp_dir" \
          >&2
        ;;
    esac
    deployed_version_temp_dir=""
  fi

  if [[ -n "$backup_temp_dir" ]]; then
    case "$backup_temp_dir" in
      "$backup_dir"/."$deployment_id".*)
        rm -rf -- "$backup_temp_dir"
        ;;
      *)
        echo "nas-deploy: refusing to remove unexpected path: $backup_temp_dir" >&2
        ;;
    esac
    backup_temp_dir=""
  fi

  if [[ -n "$forensic_temp_dir" ]]; then
    case "$forensic_temp_dir" in
      "$backup_dir"/.forensic-"$deployment_id".*)
        rm -rf -- "$forensic_temp_dir"
        ;;
      *)
        echo "nas-deploy: refusing to remove unexpected path: $forensic_temp_dir" >&2
        ;;
    esac
    forensic_temp_dir=""
  fi

  if [[ -n "$restore_temp_path" ]]; then
    case "$restore_temp_path" in
      "$database_dir"/.quantsieve-restore-"$deployment_id".*)
        rm -f -- "$restore_temp_path"
        ;;
      *)
        echo "nas-deploy: refusing to remove unexpected path: $restore_temp_path" >&2
        ;;
    esac
    restore_temp_path=""
  fi

  if [[ -n "$staging_dir" ]]; then
    case "$staging_dir" in
      "$staging_root"/"$deployment_id".*)
        rm -rf -- "$staging_dir"
        ;;
      *)
        echo "nas-deploy: refusing to remove unexpected path: $staging_dir" >&2
        ;;
    esac
    staging_dir=""
  fi
}

quiesce_created_container() {
  local container_id="$1"
  local expected_image_id="$2"
  local role="$3"

  quiesced_container_state="unknown"
  [[ "$container_id" =~ ^[[:xdigit:]]{64}$ ]] || {
    echo "nas-deploy: missing trusted $role container identity" >&2
    return 1
  }
  [[ "$expected_image_id" =~ ^sha256:[[:xdigit:]]{64}$ ]] || {
    echo "nas-deploy: missing trusted $role image identity" >&2
    return 1
  }

  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not determine $role container state" >&2
    return 1
  fi
  if [[ "$probed_container_state" == "absent" ]]; then
    quiesced_container_state="absent"
    return 0
  fi
  [[ "$probed_container_image_id" == "$expected_image_id" ]] || {
    echo "nas-deploy: refusing to stop $role after an image identity mismatch" >&2
    return 1
  }

  # The immutable container ID and image ID were captured immediately after
  # create, so a best-effort remove is safe even if the remove CLI result is
  # lost. The following independent probe is the only source of truth.
  docker rm -f "$container_id" >/dev/null 2>&1 || true
  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not verify that $role is quiesced" >&2
    return 1
  fi
  case "$probed_container_state" in
    absent)
      quiesced_container_state="absent"
      return 0
      ;;
    stopped)
      [[ "$probed_container_image_id" == "$expected_image_id" ]] || return 1
      quiesced_container_state="stopped"
      return 0
      ;;
    *)
      echo "nas-deploy: $role may still be running" >&2
      return 1
      ;;
  esac
}

ensure_original_container_quiesced() {
  local container_id="$1"
  local expected_image_id="$2"
  local role="$3"

  [[ "$container_id" =~ ^[[:xdigit:]]{64}$ ]] || return 1
  [[ "$expected_image_id" =~ ^sha256:[[:xdigit:]]{64}$ ]] || return 1
  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not determine previous $role container state" >&2
    return 1
  fi
  if [[ "$probed_container_state" == "absent" ]]; then
    return 0
  fi
  [[ "$probed_container_image_id" == "$expected_image_id" ]] || {
    echo "nas-deploy: previous $role image identity changed" >&2
    return 1
  }
  if [[ "$probed_container_state" == "stopped" ]]; then
    return 0
  fi

  docker stop --time 30 "$container_id" >/dev/null 2>&1 || true
  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not verify previous $role is stopped" >&2
    return 1
  fi
  if [[ "$probed_container_state" == "absent" ]]; then
    return 0
  fi
  [[ "$probed_container_state" == "stopped" \
    && "$probed_container_image_id" == "$expected_image_id" ]]
}

canonical_name_is_available_or_original() {
  local canonical_name="$1"
  local original_container_id="$2"
  local original_image_id="$3"

  if ! probe_container_by_name "$canonical_name"; then
    echo "nas-deploy: could not determine ownership of $canonical_name" >&2
    return 1
  fi
  [[ "$probed_container_state" == "absent" ]] && return 0
  [[ "$probed_container_id" == "$original_container_id" \
    && "$probed_container_image_id" == "$original_image_id" ]]
}

quiesce_new_production_containers() {
  local created_api_quiesced=0
  local original_api_quiesced=0
  local canonical_api_quiesced=0

  if (( new_web_created )); then
    if ! quiesce_created_container \
      "$new_web_container_id" "$new_web_image_id" "new Web"
    then
      new_container_names_clear=0
    fi
  fi

  if (( new_api_created )); then
    if quiesce_created_container \
      "$new_api_container_id" "$new_api_image_id" "new API"
    then
      created_api_quiesced=1
    else
      new_container_names_clear=0
    fi
  elif (( production_database_exposed )); then
    echo "nas-deploy: API reached production data without a trusted identity" >&2
    new_container_names_clear=0
  fi

  if (( production_database_exposed )); then
    if ensure_original_container_quiesced \
      "$original_api_container_id" "$original_api_image_id" "API"
    then
      original_api_quiesced=1
    fi
  fi

  # Query the canonical API name exactly once after the ID-based stops. A
  # failed/malformed query is unsafe for both the database and name recovery.
  if ! probe_container_by_name quantsieve-api; then
    echo "nas-deploy: could not determine ownership of quantsieve-api" >&2
    new_container_names_clear=0
  elif [[ "$probed_container_state" == "absent" ]]; then
    canonical_api_quiesced=1
  elif [[ "$probed_container_id" == "$original_api_container_id" \
    && "$probed_container_image_id" == "$original_api_image_id" ]]
  then
    [[ "$probed_container_state" == "stopped" ]] \
      && canonical_api_quiesced=1
  elif [[ "$probed_container_id" == "$new_api_container_id" \
    && "$probed_container_image_id" == "$new_api_image_id" ]]
  then
    # A failed remove can leave the owned new container stopped under the
    # canonical name. SQLite is safe, but automatic rename recovery is not.
    [[ "$probed_container_state" == "stopped" ]] \
      && canonical_api_quiesced=1
    new_container_names_clear=0
  else
    new_container_names_clear=0
  fi

  # Never restore SQLite from a negative or malformed Docker query. The
  # created API, previous API, and canonical API name must each be explicitly
  # absent or Running=false.
  if (( production_database_exposed \
    && created_api_quiesced \
    && original_api_quiesced \
    && canonical_api_quiesced ))
  then
    new_api_quiesced=1
  else
    new_api_quiesced=0
  fi

  if ! canonical_name_is_available_or_original \
    quantsieve-web "$original_web_container_id" "$original_web_image_id"
  then
    new_container_names_clear=0
  fi
}

capture_forensic_database() {
  local forensic_temp_path
  local forensic_integrity

  [[ -f "$database_path" ]] || {
    echo "nas-deploy: failed-state database is missing; no forensic backup made" >&2
    return 1
  }
  [[ ! -e "$forensic_path" ]] || {
    echo "nas-deploy: forensic backup path already exists: $forensic_path" >&2
    return 1
  }

  forensic_temp_dir="$(
    mktemp -d "$backup_dir/.forensic-${deployment_id}.XXXXXX"
  )" || return 1
  forensic_temp_path="$forensic_temp_dir/quantsieve.db"

  if ! sqlite3 "$database_path" \
    ".timeout 5000" \
    ".backup '$forensic_temp_path'"
  then
    echo "nas-deploy: could not create a consistent failed-state backup" >&2
    return 1
  fi

  forensic_integrity="$(
    sqlite3 "$forensic_temp_path" 'PRAGMA integrity_check;' 2>&1
  )" || forensic_integrity="integrity check command failed: $forensic_integrity"
  chmod 600 "$forensic_temp_path" || return 1
  mv -- "$forensic_temp_path" "$forensic_path" || return 1
  rmdir -- "$forensic_temp_dir" >/dev/null 2>&1 || true
  forensic_temp_dir=""

  echo "nas-deploy: failed-state forensic backup: $forensic_path" >&2
  echo "nas-deploy: forensic integrity result: $forensic_integrity" >&2
  return 0
}

restore_production_database() {
  [[ -f "$backup_path" ]] || {
    echo "nas-deploy: verified pre-deployment backup is missing: $backup_path" >&2
    return 1
  }
  [[ "$(sqlite3 "$backup_path" 'PRAGMA integrity_check;')" == "ok" ]] || {
    echo "nas-deploy: pre-deployment backup no longer passes integrity check" >&2
    return 1
  }

  restore_temp_path="$(
    mktemp "$database_dir/.quantsieve-restore-${deployment_id}.XXXXXX"
  )" || return 1
  cp -- "$backup_path" "$restore_temp_path" || return 1
  [[ "$(sqlite3 "$restore_temp_path" 'PRAGMA integrity_check;')" == "ok" ]] || {
    echo "nas-deploy: copied restore image failed integrity check" >&2
    return 1
  }
  chown "$database_uid:$database_gid" "$restore_temp_path" || return 1
  chmod "$database_mode" "$restore_temp_path" || return 1

  # Preparing and validating the restore image can take seconds. Re-establish
  # the fail-closed writer proof at the final mutation boundary so a daemon
  # restart or external container restart after an earlier probe cannot create
  # a TOCTOU window.
  new_api_quiesced=0
  quiesce_new_production_containers
  if (( ! new_api_quiesced )); then
    echo "nas-deploy: API writer state changed before database replacement" >&2
    return 1
  fi

  # No production API is running at this exact boundary. Remove only SQLite's
  # exact sidecars before replacing the main file on the same filesystem.
  rm -f -- "${database_path}-wal" "${database_path}-shm" || return 1
  mv -f -- "$restore_temp_path" "$database_path" || return 1
  restore_temp_path=""

  [[ "$(sqlite3 "$database_path" 'PRAGMA integrity_check;')" == "ok" ]] \
    || return 1
  [[ "$(stat -c '%u' "$database_path")" == "$database_uid" ]] || return 1
  [[ "$(stat -c '%g' "$database_path")" == "$database_gid" ]] || return 1
  [[ "$(stat -c '%a' "$database_path")" == "$database_mode" ]] || return 1
  return 0
}

stop_original_container() {
  local container_id="$1"
  local expected_image_id="$2"
  local role="$3"

  [[ "$container_id" =~ ^[[:xdigit:]]{64}$ ]] || return 1
  [[ "$expected_image_id" =~ ^sha256:[[:xdigit:]]{64}$ ]] || return 1

  # A known immutable ID is safe to target. Verify the image before stopping
  # when possible, then independently prove the final non-running state.
  if probe_container_by_id "$container_id"; then
    [[ "$probed_container_state" == "absent" ]] && return 0
    [[ "$probed_container_image_id" == "$expected_image_id" ]] || return 1
  fi
  docker stop --time 30 "$container_id" >/dev/null 2>&1 || true
  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not verify previous $role is stopped" >&2
    return 1
  fi
  [[ "$probed_container_state" == "absent" ]] && return 0
  [[ "$probed_container_state" == "stopped" \
    && "$probed_container_image_id" == "$expected_image_id" ]]
}

stop_previous_containers() {
  local stop_succeeded=1

  if (( old_api_rename_intent )); then
    stop_original_container \
      "$original_api_container_id" "$original_api_image_id" "API" \
      || stop_succeeded=0
  fi
  if (( old_web_rename_intent )); then
    stop_original_container \
      "$original_web_container_id" "$original_web_image_id" "Web" \
      || stop_succeeded=0
  fi
  (( stop_succeeded ))
}

restore_original_container() {
  local canonical_name="$1"
  local container_id="$2"
  local expected_image_id="$3"
  local was_running="$4"
  local role="$5"

  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not locate previous $role by container ID" >&2
    return 1
  fi
  [[ "$probed_container_state" != "absent" ]] || {
    echo "nas-deploy: previous $role container is missing" >&2
    return 1
  }
  [[ "$probed_container_image_id" == "$expected_image_id" ]] || {
    echo "nas-deploy: previous $role image identity changed" >&2
    return 1
  }

  if ! probe_container_by_name "$canonical_name"; then
    echo "nas-deploy: could not inspect canonical $role name" >&2
    return 1
  fi
  if [[ "$probed_container_state" == "absent" ]]; then
    # Rename may report nonzero after the daemon already committed it. Ignore
    # the CLI result and trust only the subsequent ID/name/image probe.
    docker rename "$container_id" "$canonical_name" >/dev/null 2>&1 || true
  elif [[ "$probed_container_id" != "$container_id" \
    || "$probed_container_image_id" != "$expected_image_id" ]]
  then
    echo "nas-deploy: canonical $role name is occupied by another container" >&2
    return 1
  fi

  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not verify previous $role rename" >&2
    return 1
  fi
  [[ "$probed_container_state" != "absent" \
    && "$probed_container_name" == "/$canonical_name" \
    && "$probed_container_image_id" == "$expected_image_id" ]] || {
    echo "nas-deploy: previous $role did not regain its canonical identity" >&2
    return 1
  }

  case "$was_running" in
    true)
      docker start "$container_id" >/dev/null 2>&1 || true
      ;;
    false)
      docker stop --time 30 "$container_id" >/dev/null 2>&1 || true
      ;;
    *)
      echo "nas-deploy: previous $role run state was not recorded" >&2
      return 1
      ;;
  esac
  if ! probe_container_by_id "$container_id"; then
    echo "nas-deploy: could not verify previous $role run state" >&2
    return 1
  fi
  [[ "$probed_container_name" == "/$canonical_name" \
    && "$probed_container_image_id" == "$expected_image_id" ]] || return 1
  if [[ "$was_running" == "true" ]]; then
    [[ "$probed_container_state" == "running" ]]
  else
    [[ "$probed_container_state" == "stopped" ]]
  fi
}

restore_previous_containers() {
  if (( old_api_rename_intent )); then
    restore_original_container \
      quantsieve-api \
      "$original_api_container_id" \
      "$original_api_image_id" \
      "$original_api_was_running" \
      "API" || return 1
    old_api_restored_to_canonical=1
  fi

  if (( old_web_rename_intent )); then
    restore_original_container \
      quantsieve-web \
      "$original_web_container_id" \
      "$original_web_image_id" \
      "$original_web_was_running" \
      "Web" || return 1
    old_web_restored_to_canonical=1
  fi
  return 0
}

rollback() {
  local exit_code=$?
  local database_restore_succeeded=1
  local deployed_version_restore_succeeded=0
  local services_restore_succeeded=0
  (( exit_code == 0 )) && exit_code=1
  trap - ERR INT TERM
  set +e

  echo "nas-deploy: deployment failed; entering guarded rollback" >&2
  cleanup_candidate_containers true

  new_container_names_clear=1
  # Fail closed: this may become 1 only after explicit absent/Running=false
  # proofs for every API container that could write the production database.
  new_api_quiesced=0
  quiesce_new_production_containers

  if (( production_database_exposed )); then
    database_restore_succeeded=0
    if (( new_api_quiesced )); then
      capture_forensic_database || \
        echo "nas-deploy: forensic backup failed; continuing guarded restore" >&2
      # Re-probe after the forensic snapshot. restore_production_database adds
      # one final probe after preparing its temp file and directly before the
      # sidecar removal/atomic replacement boundary.
      new_api_quiesced=0
      quiesce_new_production_containers
      if (( new_api_quiesced )); then
        if restore_production_database; then
          database_restore_succeeded=1
          echo "nas-deploy: production database restored from $backup_path" >&2
        else
          echo "nas-deploy: production database restore failed" >&2
        fi
      else
        echo "nas-deploy: API state became uncertain; database was not replaced" >&2
      fi
    else
      echo "nas-deploy: new API may still be writing; database was not replaced" >&2
    fi
  fi

  if restore_deployed_version; then
    deployed_version_restore_succeeded=1
  else
    echo "nas-deploy: DEPLOYED_VERSION restore failed" >&2
  fi

  if (( database_restore_succeeded \
    && deployed_version_restore_succeeded \
    && new_container_names_clear ))
  then
    if restore_previous_containers; then
      services_restore_succeeded=1
    fi
  fi

  if (( ! services_restore_succeeded )); then
    stop_previous_containers
    echo "nas-deploy: previous services remain stopped for manual recovery" >&2
    echo "nas-deploy: verified pre-deployment backup: $backup_path" >&2
    if [[ -f "$forensic_path" ]]; then
      echo "nas-deploy: failed-state forensic backup: $forensic_path" >&2
    fi
    if (( old_api_restored_to_canonical )); then
      echo "nas-deploy: previous API container: quantsieve-api (stopped)" >&2
    else
      echo "nas-deploy: previous API container: $old_api_name" >&2
    fi
    if (( old_web_restored_to_canonical )); then
      echo "nas-deploy: previous Web container: quantsieve-web (stopped)" >&2
    else
      echo "nas-deploy: previous Web container: $old_web_name" >&2
    fi
  else
    echo "nas-deploy: previous API/Web containers restored" >&2
  fi

  cleanup_generated_directories
  if [[ -f "$backup_path" ]]; then
    echo "nas-deploy: pre-deployment backup retained at $backup_path" >&2
  else
    echo "nas-deploy: failure occurred before a database backup was published" >&2
  fi
  exit "$exit_code"
}

trap rollback ERR INT TERM

assert_container_absent() {
  local container_name="$1"
  probe_container_by_name "$container_name" \
    || die "could not determine whether container name is free: $container_name"
  [[ "$probed_container_state" == "absent" ]] \
    || die "container name is already in use: $container_name"
}

assert_container_running() {
  local container_name="$1"
  probe_container_by_name "$container_name" \
    || die "could not inspect container: $container_name"
  [[ "$probed_container_state" == "running" ]] \
    || die "container is not running: $container_name"
}

assert_image_metadata() {
  local image="$1"
  local maximum_layers="$2"
  local expected_user="$3"
  local expected_workdir="$4"
  local actual_layers
  local actual_user
  local actual_workdir

  docker image inspect "$image" >/dev/null
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

read_api_image_provenance() {
  local image_id="$1"
  local source_revision
  local dependency_lock_sha256

  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "cannot read provenance from an unverified API image identity"

  source_revision="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "$image_id"
  )" || die "could not read the API source revision label"
  dependency_lock_sha256="$(
    docker image inspect \
      --format '{{ index .Config.Labels "com.quantsieve.dependency-lock-sha256" }}' \
      "$image_id"
  )" || die "could not read the API dependency lock label"

  [[ "$source_revision" =~ ^[0-9a-f]{64}$ ]] \
    || die "API source revision label must be a 64-character lowercase SHA-256"
  [[ "$dependency_lock_sha256" =~ ^[0-9a-f]{64}$ ]] \
    || die "API dependency lock label must be a 64-character lowercase SHA-256"

  api_source_revision="$source_revision"
  api_dependency_lock_sha256="$dependency_lock_sha256"
}

read_web_image_provenance() {
  local image_id="$1"
  local expected_source_revision="$2"
  local source_revision

  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "cannot read provenance from an unverified Web image identity"
  [[ "$expected_source_revision" =~ ^[0-9a-f]{64}$ ]] \
    || die "cannot compare Web provenance to an invalid API source revision"

  source_revision="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "$image_id"
  )" || die "could not read the Web source revision label"

  [[ "$source_revision" =~ ^[0-9a-f]{64}$ ]] \
    || die "Web source revision label must be a 64-character lowercase SHA-256"
  [[ "$source_revision" == "$expected_source_revision" ]] \
    || die "API and Web image source revision labels do not match"

  web_source_revision="$source_revision"
}

wait_for_api_health() {
  local container_name="$1"
  local attempt

  for (( attempt = 1; attempt <= health_attempts; attempt++ )); do
    if docker exec "$container_name" python -c \
      'import json, sys, urllib.request
payload = json.load(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3))
raise SystemExit(0 if payload.get("status") == "ok" and payload.get("version") == sys.argv[1] else 1)' \
      "$tag" >/dev/null 2>&1
    then
      return 0
    fi
    sleep "$health_interval_seconds"
  done

  echo "nas-deploy: API health/version check failed for $container_name" >&2
  return 1
}

assert_api_contracts() {
  local container_name="$1"

  if ! docker exec "$container_name" python -c \
    'import json, os, sqlite3, urllib.request
from quantsieve_api.paper_oms import _SCHEMA_VERSION

catalog = json.load(
    urllib.request.urlopen(
        "http://127.0.0.1:8000/api/v1/factors/catalog",
        timeout=3,
    )
)
assert catalog.get("schema_version") == 1
assert {item.get("factor_id") for item in catalog.get("recipes", [])} == {
    "momentum",
    "reversal",
    "low_volatility",
    "volume_surprise",
}
database = sqlite3.connect(os.environ["QUANTSIEVE_DATABASE_PATH"])
try:
    assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.execute(
        "SELECT singleton, schema_version FROM paper_oms_meta"
    ).fetchall() == [(1, _SCHEMA_VERSION)]
finally:
    database.close()' \
    >/dev/null 2>&1
  then
    echo \
      "nas-deploy: API factor/Paper OMS contract check failed for $container_name" \
      >&2
    return 1
  fi
}

wait_for_translation_health() {
  local container_name="$1"
  local attempt

  [[ "$translation_enabled" == "true" ]] || return 0
  for (( attempt = 1; attempt <= health_attempts; attempt++ )); do
    if docker exec "$container_name" python -c \
      'import json, os, re, urllib.request
status = json.load(
    urllib.request.urlopen(
        "http://127.0.0.1:8000/api/v1/monitor/status",
        timeout=3,
    )
)
translation = status.get("translation", {})
assert translation.get("enabled") is True
base_url = os.environ.get(
    "QUANTSIEVE_TRANSLATION_BASE_URL",
    "http://translate:5000",
).rstrip("/")
payload = json.dumps({
    "q": "Federal Reserve holds rates steady.",
    "source": "en",
    "target": "zh",
    "format": "text",
}).encode()
request = urllib.request.Request(
    f"{base_url}/translate",
    data=payload,
    headers={"Content-Type": "application/json"},
)
response = json.load(urllib.request.urlopen(request, timeout=20))
translated = response.get("translatedText")
assert isinstance(translated, str) and re.search(r"[\u3400-\u9fff]", translated)
events = json.load(
    urllib.request.urlopen(
        "http://127.0.0.1:8000/api/v1/monitor/feed?limit=100",
        timeout=5,
    )
)
event = next(
    (
        item
        for item in events
        if re.search(r"[A-Za-z]{4}", str(item.get("title", "")))
    ),
    None,
)
if event is not None:
    route_payload = json.dumps({
        "items": [{
            "source": event["source"],
            "source_id": event["source_id"],
        }]
    }).encode()
    route_request = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/monitor/translations",
        data=route_payload,
        headers={"Content-Type": "application/json"},
    )
    route_response = json.load(
        urllib.request.urlopen(route_request, timeout=60)
    )
    item = route_response["items"][0]
    assert item["status"] in {"translated", "identity", "partial"}
    assert isinstance(item.get("title_zh"), str)
    assert re.search(r"[\u3400-\u9fff]", item["title_zh"])' \
      >/dev/null 2>&1
    then
      return 0
    fi
    sleep "$health_interval_seconds"
  done

  echo "nas-deploy: enabled translation path failed for $container_name" >&2
  return 1
}

wait_for_internal_web_health() {
  local api_container_name="$1"
  local web_container_name="$2"
  local attempt

  for (( attempt = 1; attempt <= health_attempts; attempt++ )); do
    if docker exec "$api_container_name" python -c \
      'import json, sys, urllib.request
url = f"http://{sys.argv[1]}:3000/backend/health"
payload = json.load(urllib.request.urlopen(url, timeout=3))
raise SystemExit(0 if payload.get("status") == "ok" and payload.get("version") == sys.argv[2] else 1)' \
      "$web_container_name" "$tag" >/dev/null 2>&1
    then
      return 0
    fi
    sleep "$health_interval_seconds"
  done

  echo "nas-deploy: internal Web health/version check failed" >&2
  return 1
}

wait_for_external_web_health() {
  local attempt
  local payload
  local compact_payload

  for (( attempt = 1; attempt <= health_attempts; attempt++ )); do
    if payload="$(curl --fail --silent --show-error \
      --max-time 5 "http://$host_ip:$web_port/backend/health" 2>/dev/null)"
    then
      compact_payload="$(printf '%s' "$payload" | tr -d '[:space:]')"
      if [[ "$compact_payload" == *'"status":"ok"'* \
        && "$compact_payload" == *"\"version\":\"$tag\""* ]]
      then
        return 0
      fi
    fi
    sleep "$health_interval_seconds"
  done

  echo "nas-deploy: external Web health/version check failed" >&2
  return 1
}

create_candidate_api() {
  if docker create \
    --name "$candidate_api_name" \
    --network "$network" \
    --network-alias "$candidate_api_name" \
    --label "com.quantsieve.deployment=$deployment_id" \
    --label "com.quantsieve.role=api-candidate" \
    --mount "type=bind,src=$staging_dir,dst=/app/data" \
    --env QUANTSIEVE_ENVIRONMENT=production \
    --env "QUANTSIEVE_BUILD_VERSION=$tag" \
    --env "QUANTSIEVE_SOURCE_REVISION=$api_source_revision" \
    --env "QUANTSIEVE_DEPENDENCY_LOCK_SHA256=$api_dependency_lock_sha256" \
    --env "QUANTSIEVE_DATABASE_PATH=$container_database_path" \
    --env QUANTSIEVE_CACHE_PATH=/app/data/cache.db \
    --env "QUANTSIEVE_CORS_ORIGINS=$cors_origins" \
    --env QUANTSIEVE_MONITOR_SCHEDULER_ENABLED=false \
    --env "QUANTSIEVE_MONITOR_POLL_SECONDS=$monitor_poll_seconds" \
    --env QUANTSIEVE_PAPER_SCHEDULER_ENABLED=false \
    --env QUANTSIEVE_PORTFOLIO_OPENING_SCHEDULER_ENABLED=false \
    --env QUANTSIEVE_PORTFOLIO_SETTLEMENT_SCHEDULER_ENABLED=false \
    "${optional_api_env_args[@]}" \
    "$new_api_image_id" >/dev/null
  then
    candidate_api_created=1
  else
    if container_matches_deployment "$candidate_api_name" "api-candidate"; then
      candidate_api_created=1
    fi
    return 1
  fi
  docker start "$candidate_api_name" >/dev/null
}

create_candidate_web() {
  if docker create \
    --name "$candidate_web_name" \
    --network "$network" \
    --label "com.quantsieve.deployment=$deployment_id" \
    --label "com.quantsieve.role=web-candidate" \
    --env "QUANTSIEVE_API_INTERNAL_URL=http://$candidate_api_name:8000" \
    "$new_web_image_id" >/dev/null
  then
    candidate_web_created=1
  else
    if container_matches_deployment "$candidate_web_name" "web-candidate"; then
      candidate_web_created=1
    fi
    return 1
  fi
  docker start "$candidate_web_name" >/dev/null
}

create_production_api() {
  local create_reported_success=0

  if docker create \
    --name quantsieve-api \
    --restart unless-stopped \
    --network "$network" \
    --network-alias api \
    --label "com.quantsieve.deployment=$deployment_id" \
    --label "com.quantsieve.role=api" \
    --mount "type=bind,src=$data_dir,dst=/app/data" \
    --env QUANTSIEVE_ENVIRONMENT=production \
    --env "QUANTSIEVE_BUILD_VERSION=$tag" \
    --env "QUANTSIEVE_SOURCE_REVISION=$api_source_revision" \
    --env "QUANTSIEVE_DEPENDENCY_LOCK_SHA256=$api_dependency_lock_sha256" \
    --env "QUANTSIEVE_DATABASE_PATH=$container_database_path" \
    --env "QUANTSIEVE_CACHE_PATH=$cache_path" \
    --env "QUANTSIEVE_CORS_ORIGINS=$cors_origins" \
    --env "QUANTSIEVE_MONITOR_SCHEDULER_ENABLED=$monitor_scheduler_enabled" \
    --env "QUANTSIEVE_MONITOR_POLL_SECONDS=$monitor_poll_seconds" \
    --env "QUANTSIEVE_PAPER_SCHEDULER_ENABLED=$paper_scheduler_enabled" \
    --env \
      "QUANTSIEVE_PORTFOLIO_OPENING_SCHEDULER_ENABLED=$portfolio_opening_scheduler_enabled" \
    --env \
      "QUANTSIEVE_PORTFOLIO_SETTLEMENT_SCHEDULER_ENABLED=$portfolio_settlement_scheduler_enabled" \
    "${optional_api_env_args[@]}" \
    "$new_api_image_id" >/dev/null
  then
    create_reported_success=1
    new_api_created=1
  fi
  if record_created_container_identity \
    quantsieve-api "$new_api_image_id" "api"
  then
    new_api_created=1
    new_api_container_id="$recorded_created_container_id"
  elif (( create_reported_success )); then
    echo "nas-deploy: could not record the new API container identity" >&2
    return 1
  fi
  # A nonzero create can arrive after the daemon committed the container. Its
  # identity is retained for rollback, but the deployment still fails.
  (( create_reported_success )) || return 1

  # From this point onward the new process may migrate or write production
  # SQLite state, even if docker start ultimately reports an error.
  production_database_exposed=1
  docker start "$new_api_container_id" >/dev/null
}

create_production_web() {
  local create_reported_success=0

  if docker create \
    --name quantsieve-web \
    --restart unless-stopped \
    --network "$network" \
    --label "com.quantsieve.deployment=$deployment_id" \
    --label "com.quantsieve.role=web" \
    --publish "$host_ip:$web_port:3000" \
    --env QUANTSIEVE_API_INTERNAL_URL=http://api:8000 \
    "$new_web_image_id" >/dev/null
  then
    create_reported_success=1
    new_web_created=1
  fi
  if record_created_container_identity \
    quantsieve-web "$new_web_image_id" "web"
  then
    new_web_created=1
    new_web_container_id="$recorded_created_container_id"
  elif (( create_reported_success )); then
    echo "nas-deploy: could not record the new Web container identity" >&2
    return 1
  fi
  (( create_reported_success )) || return 1
  docker start "$new_web_container_id" >/dev/null
}

docker info >/dev/null
docker network inspect "$network" >/dev/null
assert_container_running quantsieve-api
original_api_container_id="$probed_container_id"
original_api_image_id="$probed_container_image_id"
original_api_was_running="true"
assert_container_running quantsieve-web
original_web_container_id="$probed_container_id"
original_web_image_id="$probed_container_image_id"
original_web_was_running="true"
snapshot_deployed_version

for reserved_name in \
  "$old_api_name" "$old_web_name" "$candidate_api_name" "$candidate_web_name"
do
  assert_container_absent "$reserved_name"
done

new_api_image_id="$(docker image inspect --format '{{.Id}}' "$api_image")"
new_web_image_id="$(docker image inspect --format '{{.Id}}' "$web_image")"
[[ "$new_api_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "could not record the new API image identity"
[[ "$new_web_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "could not record the new Web image identity"

assert_image_metadata \
  "$new_api_image_id" "$max_api_layers" "$expected_api_user" "$expected_api_workdir"
assert_image_metadata \
  "$new_web_image_id" "$max_web_layers" "$expected_web_user" "$expected_web_workdir"
read_api_image_provenance "$new_api_image_id"
read_web_image_provenance "$new_web_image_id" "$api_source_revision"
readonly api_source_revision
readonly api_dependency_lock_sha256
readonly web_source_revision

docker run --rm --entrypoint python "$new_api_image_id" -c \
  'import quantsieve_api, quantsieve_engine, quantsieve_monitor, quantsieve_providers'

api_uid="$(docker run --rm --entrypoint id "$new_api_image_id" -u)"
api_gid="$(docker run --rm --entrypoint id "$new_api_image_id" -g)"
[[ "$api_uid" =~ ^[0-9]+$ && "$api_gid" =~ ^[0-9]+$ ]] \
  || die "could not determine the API image UID/GID"

readonly -a api_environment_whitelist=(
  QUANTSIEVE_LLM_API_KEY
  QUANTSIEVE_LLM_BASE_URL
  QUANTSIEVE_LLM_MODEL
  QUANTSIEVE_SEC_USER_AGENT
  QUANTSIEVE_X_API_BASE_URL
  QUANTSIEVE_X_BEARER_TOKEN
  QUANTSIEVE_TRANSLATION_ENABLED
  QUANTSIEVE_TRANSLATION_BASE_URL
  QUANTSIEVE_TRANSLATION_TIMEOUT_SECONDS
  QUANTSIEVE_TRANSLATION_BATCH_LIMIT
  QUANTSIEVE_TRANSLATION_CACHE_TTL_DAYS
  QUANTSIEVE_TRANSLATION_CONTRACT_VERSION
  QUANTSIEVE_FACTOR_RESEARCH_MAX_CONCURRENCY
  QUANTSIEVE_FACTOR_RESEARCH_PROVIDER_MAX_CONCURRENCY
  QUANTSIEVE_FACTOR_RESEARCH_DEADLINE_SECONDS
  QUANTSIEVE_MONITOR_SCHEDULER_ENABLED
  QUANTSIEVE_MONITOR_POLL_SECONDS
  QUANTSIEVE_PAPER_SCHEDULER_ENABLED
  QUANTSIEVE_PAPER_POLL_SECONDS
  QUANTSIEVE_PORTFOLIO_OPENING_SCHEDULER_ENABLED
  QUANTSIEVE_PORTFOLIO_OPENING_POLL_SECONDS
  QUANTSIEVE_PORTFOLIO_OPENING_QUOTE_DEADLINE_SECONDS
  QUANTSIEVE_PORTFOLIO_OPENING_LEASE_SECONDS
  QUANTSIEVE_PORTFOLIO_SETTLEMENT_SCHEDULER_ENABLED
  QUANTSIEVE_PORTFOLIO_SETTLEMENT_POLL_SECONDS
  QUANTSIEVE_PORTFOLIO_SETTLEMENT_HISTORY_DEADLINE_SECONDS
  QUANTSIEVE_PORTFOLIO_SETTLEMENT_LEASE_SECONDS
)
inherit_api_environment "$original_api_container_id"

# Preserve explicit/current scheduler settings. When neither exists, match the
# application defaults (notably monitor scheduling is disabled by default).
readonly monitor_scheduler_enabled="${QUANTSIEVE_MONITOR_SCHEDULER_ENABLED-false}"
readonly monitor_poll_seconds="${QUANTSIEVE_MONITOR_POLL_SECONDS-60}"
readonly paper_scheduler_enabled="${QUANTSIEVE_PAPER_SCHEDULER_ENABLED-true}"
readonly translation_enabled="${QUANTSIEVE_TRANSLATION_ENABLED-false}"
readonly portfolio_opening_scheduler_enabled="${QUANTSIEVE_PORTFOLIO_OPENING_SCHEDULER_ENABLED-false}"
readonly portfolio_settlement_scheduler_enabled="${QUANTSIEVE_PORTFOLIO_SETTLEMENT_SCHEDULER_ENABLED-false}"

require_positive_integer "QUANTSIEVE_MONITOR_POLL_SECONDS" "$monitor_poll_seconds"
[[ "$monitor_scheduler_enabled" =~ ^(true|false)$ ]] \
  || die "QUANTSIEVE_MONITOR_SCHEDULER_ENABLED must be true or false"
[[ "$paper_scheduler_enabled" =~ ^(true|false)$ ]] \
  || die "QUANTSIEVE_PAPER_SCHEDULER_ENABLED must be true or false"
[[ "$translation_enabled" =~ ^(true|false)$ ]] \
  || die "QUANTSIEVE_TRANSLATION_ENABLED must be true or false"
[[ "$portfolio_opening_scheduler_enabled" =~ ^(true|false)$ ]] \
  || die "QUANTSIEVE_PORTFOLIO_OPENING_SCHEDULER_ENABLED must be true or false"
[[ "$portfolio_settlement_scheduler_enabled" =~ ^(true|false)$ ]] \
  || die "QUANTSIEVE_PORTFOLIO_SETTLEMENT_SCHEDULER_ENABLED must be true or false"

optional_api_env_args=()
for optional_env_name in \
  QUANTSIEVE_LLM_API_KEY \
  QUANTSIEVE_LLM_BASE_URL \
  QUANTSIEVE_LLM_MODEL \
  QUANTSIEVE_SEC_USER_AGENT \
  QUANTSIEVE_X_API_BASE_URL \
  QUANTSIEVE_X_BEARER_TOKEN \
  QUANTSIEVE_TRANSLATION_ENABLED \
  QUANTSIEVE_TRANSLATION_BASE_URL \
  QUANTSIEVE_TRANSLATION_TIMEOUT_SECONDS \
  QUANTSIEVE_TRANSLATION_BATCH_LIMIT \
  QUANTSIEVE_TRANSLATION_CACHE_TTL_DAYS \
  QUANTSIEVE_TRANSLATION_CONTRACT_VERSION \
  QUANTSIEVE_FACTOR_RESEARCH_MAX_CONCURRENCY \
  QUANTSIEVE_FACTOR_RESEARCH_PROVIDER_MAX_CONCURRENCY \
  QUANTSIEVE_FACTOR_RESEARCH_DEADLINE_SECONDS \
  QUANTSIEVE_PAPER_POLL_SECONDS \
  QUANTSIEVE_PORTFOLIO_OPENING_POLL_SECONDS \
  QUANTSIEVE_PORTFOLIO_OPENING_QUOTE_DEADLINE_SECONDS \
  QUANTSIEVE_PORTFOLIO_OPENING_LEASE_SECONDS \
  QUANTSIEVE_PORTFOLIO_SETTLEMENT_POLL_SECONDS \
  QUANTSIEVE_PORTFOLIO_SETTLEMENT_HISTORY_DEADLINE_SECONDS \
  QUANTSIEVE_PORTFOLIO_SETTLEMENT_LEASE_SECONDS
do
  if [[ -v "$optional_env_name" ]]; then
    optional_api_env_args+=(--env "$optional_env_name")
  fi
done

database_uid="$(stat -c '%u' "$database_path")"
database_gid="$(stat -c '%g' "$database_path")"
database_mode="$(stat -c '%a' "$database_path")"
[[ "$database_uid" =~ ^[0-9]+$ && "$database_gid" =~ ^[0-9]+$ ]] \
  || die "could not record database ownership"
[[ "$database_mode" =~ ^[0-7]{3,4}$ ]] \
  || die "could not record database permissions"

[[ "$(sqlite3 "$database_path" 'PRAGMA integrity_check;')" == "ok" ]] \
  || die "production database failed its pre-deployment integrity check"
[[ ! -e "$backup_path" ]] || die "backup path already exists: $backup_path"
[[ ! -e "$forensic_path" ]] || die "forensic path already exists: $forensic_path"

backup_temp_dir="$(mktemp -d "$backup_dir/.${deployment_id}.XXXXXX")"
readonly backup_temp_path="$backup_temp_dir/quantsieve.db"
sqlite3 "$database_path" \
  ".timeout 5000" \
  ".backup '$backup_temp_path'"
if [[ "$(sqlite3 "$backup_temp_path" 'PRAGMA integrity_check;')" != "ok" ]]; then
  echo "nas-deploy: temporary database backup failed its integrity check" >&2
  false
fi
chmod 600 "$backup_temp_path"
mv -- "$backup_temp_path" "$backup_path"
rmdir -- "$backup_temp_dir"
backup_temp_dir=""

staging_dir="$(mktemp -d "$staging_root/${deployment_id}.XXXXXX")"
candidate_database_path="$staging_dir/$database_relative_path"
candidate_database_dir="$(dirname -- "$candidate_database_path")"
mkdir -p -- "$candidate_database_dir"
cp -- "$backup_path" "$candidate_database_path"
chown -R "$api_uid:$api_gid" "$staging_dir"
chmod 700 "$staging_dir"
chmod 600 "$candidate_database_path"

create_candidate_api
wait_for_api_health "$candidate_api_name"
assert_api_contracts "$candidate_api_name"
wait_for_translation_health "$candidate_api_name"
create_candidate_web
wait_for_internal_web_health "$candidate_api_name" "$candidate_web_name"
cleanup_candidate_containers

old_web_rename_intent=1
docker rename "$original_web_container_id" "$old_web_name"
old_web_renamed=1
docker stop --time 30 "$original_web_container_id" >/dev/null

old_api_rename_intent=1
docker rename "$original_api_container_id" "$old_api_name"
old_api_renamed=1
docker stop --time 30 "$original_api_container_id" >/dev/null

create_production_api
wait_for_api_health quantsieve-api
assert_api_contracts quantsieve-api
wait_for_translation_health quantsieve-api
create_production_web
wait_for_internal_web_health quantsieve-api quantsieve-web
wait_for_external_web_health

if [[ "$(sqlite3 "$database_path" 'PRAGMA integrity_check;')" != "ok" ]]; then
  echo "nas-deploy: production database failed its post-deployment integrity check" >&2
  false
fi

publish_deployed_version
verify_written_deployed_version

# Commit the container switch before any cleanup. The stopped previous containers
# remain available for an explicit manual rollback.
trap - ERR INT TERM
cleanup_generated_directories || true

echo "nas-deploy: deployment succeeded"
echo "  release: $tag"
echo "  backup: $backup_path"
echo "  previous API: $old_api_name (stopped, retained)"
echo "  previous Web: $old_web_name (stopped, retained)"
