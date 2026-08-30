#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

die() {
  echo "nas-translation: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

require_boolean() {
  local name="$1"
  local value="$2"

  [[ "$value" =~ ^(true|false)$ ]] || die "$name must be true or false"
}

require_safe_absolute_path() {
  local name="$1"
  local path="$2"

  [[ "$path" == /* ]] || die "$name must be an absolute path"
  [[ "$path" != *$'\n'* && "$path" != *$'\r'* && "$path" != *,* ]] \
    || die "$name contains a forbidden character"
}

reject_symlink_components() {
  local name="$1"
  local path="$2"
  local component
  local current=""
  local -a components=()

  IFS='/' read -r -a components <<< "${path#/}"
  for component in "${components[@]}"; do
    [[ -n "$component" ]] || continue
    current="$current/$component"
    [[ ! -L "$current" ]] || die "$name contains a symbolic-link component: $current"
  done
}

container_is_running() {
  local container_id="$1"
  [[ "$(docker container inspect --format '{{.State.Running}}' "$container_id")" == "true" ]]
}

container_is_on_network() {
  local container_id="$1"
  local endpoint_id

  endpoint_id="$(
    docker container inspect \
      --format "{{with index .NetworkSettings.Networks \"$network\"}}{{.NetworkID}}{{end}}" \
      "$container_id"
  )" || return 1
  [[ -n "$endpoint_id" ]]
}

container_has_network_alias() {
  local container_id="$1"
  local alias
  local aliases

  aliases="$(
    docker container inspect \
      --format "{{with index .NetworkSettings.Networks \"$network\"}}{{range .Aliases}}{{println .}}{{end}}{{end}}" \
      "$container_id"
  )" || return 1
  while IFS= read -r alias; do
    [[ "$alias" == "$stable_alias" ]] && return 0
  done <<< "$aliases"
  return 1
}

assert_stable_alias_exclusive() {
  local allowed_container_id="${1:-}"
  local container_id
  local container_ids
  local normalized_id
  local owner_name

  container_ids="$(
    docker network inspect \
      --format '{{range $id, $_ := .Containers}}{{println $id}}{{end}}' \
      "$network"
  )" || die "could not enumerate containers attached to $network"

  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    normalized_id="$(
      docker container inspect --format '{{.Id}}' "$container_id"
    )" || die "could not inspect a container attached to $network"
    if container_has_network_alias "$normalized_id"; then
      if [[ -n "$allowed_container_id" \
        && "$normalized_id" == "$allowed_container_id" ]]
      then
        continue
      fi
      owner_name="$(
        docker container inspect --format '{{.Name}}' "$normalized_id"
      )" || die "could not identify the owner of network alias $stable_alias"
      owner_name="${owner_name#/}"
      die "network alias $stable_alias is already owned by $owner_name ($normalized_id)"
    fi
  done <<< "$container_ids"
}

disconnect_container_from_network() {
  local container_id="$1"

  container_is_on_network "$container_id" || return 0
  if container_is_running "$container_id"; then
    docker network disconnect "$network" "$container_id"
  else
    docker network disconnect --force "$network" "$container_id"
  fi
}

assert_container_name_absent() {
  local name="$1"

  if docker container inspect "$name" >/dev/null 2>&1; then
    die "reserved container name already exists: $name"
  fi
}

wait_for_languages() {
  local container_id="$1"
  local attempt

  for (( attempt = 1; attempt <= 120; attempt++ )); do
    if docker exec "$container_id" /app/venv/bin/python -c \
      'import json
import urllib.request
languages = json.load(
    urllib.request.urlopen("http://127.0.0.1:5000/languages", timeout=3)
)
assert any(
    item.get("code") == "en"
    and "zh-Hans" in item.get("targets", [])
    for item in languages
)' >/dev/null 2>&1
    then
      return 0
    fi
    sleep 1
  done

  docker logs --tail 80 "$container_id" >&2 || true
  echo "nas-translation: candidate language endpoint did not become ready" >&2
  return 1
}

smoke_translation_in_container() {
  local container_id="$1"

  docker exec "$container_id" /app/venv/bin/python -c \
    'import json
import re
import urllib.request
source = "Federal Reserve holds rates steady."
payload = json.dumps({
    "q": source,
    "source": "en",
    "target": "zh",
    "format": "text",
}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:5000/translate",
    data=payload,
    headers={"Content-Type": "application/json"},
)
response = json.load(urllib.request.urlopen(request, timeout=20))
translated = response.get("translatedText")
assert isinstance(translated, str)
translated = translated.strip()
assert translated and translated != source
assert re.search(r"[\u3400-\u9fff]", translated)'
}

smoke_translation_from_probe() {
  local base_url="$1"

  docker exec "$probe_container_id" python -c \
    'import json
import re
import sys
import urllib.request
base_url = sys.argv[1].rstrip("/")
languages = json.load(
    urllib.request.urlopen(f"{base_url}/languages", timeout=3)
)
assert any(
    item.get("code") == "en"
    and "zh-Hans" in item.get("targets", [])
    for item in languages
)
source = "Federal Reserve holds rates steady."
payload = json.dumps({
    "q": source,
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
assert isinstance(translated, str)
translated = translated.strip()
assert translated and translated != source
assert re.search(r"[\u3400-\u9fff]", translated)' \
    "$base_url"
}

validate_existing_canonical_container() {
  local role_label
  local managed_by_label
  local labels_json
  local mount_source
  local port_bindings
  local old_image_id

  role_label="$(
    docker container inspect \
      --format '{{if .Config.Labels}}{{index .Config.Labels "com.quantsieve.role"}}{{end}}' \
      "$old_container_id"
  )"
  managed_by_label="$(
    docker container inspect \
      --format '{{if .Config.Labels}}{{index .Config.Labels "com.quantsieve.managed-by"}}{{end}}' \
      "$old_container_id"
  )"
  labels_json="$(
    docker container inspect --format '{{json .Config.Labels}}' "$old_container_id"
  )"
  mount_source="$(
    docker container inspect \
      --format '{{range .Mounts}}{{if eq .Destination "/home/libretranslate/.local"}}{{.Source}}{{end}}{{end}}' \
      "$old_container_id"
  )"
  port_bindings="$(
    docker container inspect --format '{{json .HostConfig.PortBindings}}' "$old_container_id"
  )"

  [[ "$mount_source" == "$model_dir" ]] \
    || die "existing canonical container uses an unexpected model mount"
  container_is_on_network "$old_container_id" \
    || die "existing canonical container is not attached to $network"
  container_has_network_alias "$old_container_id" \
    || die "existing canonical container does not own the $stable_alias alias"
  [[ "$port_bindings" == "null" || "$port_bindings" == "{}" ]] \
    || die "existing canonical container unexpectedly publishes host ports"

  if [[ "$role_label" == "translation" \
    && "$managed_by_label" == "nas-translation" ]]
  then
    return 0
  fi

  [[ "$adopt_legacy" == "true" ]] \
    || die "existing canonical container is unlabeled; set QUANTSIEVE_TRANSLATION_ADOPT_LEGACY=true only after verifying it"
  [[ -z "$role_label" && -z "$managed_by_label" ]] \
    || die "legacy adoption refuses a container carrying foreign ownership labels"
  [[ "$labels_json" == "null" || "$labels_json" == "{}" ]] \
    || die "legacy adoption requires the existing container to have no labels"
  old_image_id="$(
    docker container inspect --format '{{.Image}}' "$old_container_id"
  )"
  [[ "$old_image_id" == "$target_image_id" ]] \
    || die "legacy adoption requires the existing container to use the pinned image"

  echo "nas-translation: explicitly adopting verified unlabeled legacy container"
}

cleanup_temporary_model() {
  if [[ -n "$temporary_model" && -f "$temporary_model" ]]; then
    case "$temporary_model" in
      "$model_dir"/.translation-model.*)
        rm -f -- "$temporary_model"
        ;;
      *)
        echo "nas-translation: refusing to remove unexpected temporary path" >&2
        return 1
        ;;
    esac
  fi
}

cleanup_candidate_only() {
  [[ -n "$candidate_container_id" ]] || return 0
  docker container inspect "$candidate_container_id" >/dev/null 2>&1 || return 0
  docker update --restart no "$candidate_container_id" >/dev/null 2>&1 || true
  docker rm -f "$candidate_container_id" >/dev/null
}

rollback_switch() {
  local rollback_failed=0
  local current_name=""
  local current_running=""

  echo "nas-translation: deployment failed; restoring previous translation service" >&2

  if ! cleanup_candidate_only; then
    echo "nas-translation: could not remove failed candidate container" >&2
    rollback_failed=1
  fi

  if (( old_container_exists )); then
    if ! docker container inspect "$old_container_id" >/dev/null 2>&1; then
      echo "nas-translation: previous container identity is missing: $old_container_id" >&2
      rollback_failed=1
    else
      current_name="$(
        docker container inspect --format '{{.Name}}' "$old_container_id"
      )"
      current_name="${current_name#/}"
      if [[ "$current_name" != "$container_name" ]]; then
        if docker container inspect "$container_name" >/dev/null 2>&1; then
          echo "nas-translation: canonical name is still occupied during rollback" >&2
          rollback_failed=1
        elif ! docker rename "$old_container_id" "$container_name"; then
          echo "nas-translation: could not restore canonical container name" >&2
          rollback_failed=1
        fi
      fi

      if ! container_is_on_network "$old_container_id"; then
        if ! docker network connect \
          --alias "$stable_alias" "$network" "$old_container_id"
        then
          echo "nas-translation: could not restore previous network attachment" >&2
          rollback_failed=1
        fi
      elif ! container_has_network_alias "$old_container_id"; then
        if ! disconnect_container_from_network "$old_container_id" \
          || ! docker network connect \
            --alias "$stable_alias" "$network" "$old_container_id"
        then
          echo "nas-translation: could not restore previous network alias" >&2
          rollback_failed=1
        fi
      fi

      current_running="$(
        docker container inspect --format '{{.State.Running}}' "$old_container_id"
      )"
      if [[ "$old_container_was_running" == "true" \
        && "$current_running" != "true" ]]
      then
        if ! docker start "$old_container_id" >/dev/null; then
          echo "nas-translation: could not restart previous container" >&2
          rollback_failed=1
        fi
      elif [[ "$old_container_was_running" == "false" \
        && "$current_running" == "true" ]]
      then
        if ! docker stop --time 30 "$old_container_id" >/dev/null; then
          echo "nas-translation: could not restore previous stopped state" >&2
          rollback_failed=1
        fi
      fi

      if [[ "$old_container_was_running" == "true" ]] \
        && ! smoke_translation_from_probe "http://$stable_alias:5000" \
          >/dev/null 2>&1
      then
        echo "nas-translation: restored service failed its cross-container smoke test" >&2
        rollback_failed=1
      fi
    fi
  fi

  if (( rollback_failed )); then
    echo "nas-translation: automatic rollback was incomplete; inspect these IDs:" >&2
    echo "  previous: ${old_container_id:-none}" >&2
    echo "  candidate: ${candidate_container_id:-none}" >&2
    return 1
  fi
  echo "nas-translation: previous translation service restored" >&2
}

on_exit() {
  local status=$?

  trap - EXIT HUP INT TERM
  set +e
  cleanup_temporary_model
  if (( status != 0 && deployment_succeeded == 0 )); then
    if (( commit_started )); then
      rollback_switch || true
    else
      cleanup_candidate_only || \
        echo "nas-translation: failed candidate requires manual cleanup" >&2
    fi
  fi
  exit "$status"
}

for required_command in \
  awk chmod chown curl date docker flock mkdir mktemp mv realpath rm \
  sha256sum sleep
do
  require_command "$required_command"
done

readonly root_dir_input="${QUANTSIEVE_ROOT_DIR:-/srv/quantsieve}"
readonly network="${QUANTSIEVE_DOCKER_NETWORK:-quantsieve}"
readonly lock_file_input="${QUANTSIEVE_LOCK_FILE:-$root_dir_input/release.lock}"
readonly model_dir_input="${QUANTSIEVE_TRANSLATION_MODEL_DIR:-$root_dir_input/translate-models}"
readonly container_name="${QUANTSIEVE_TRANSLATION_CONTAINER:-quantsieve-translate}"
readonly probe_container="${QUANTSIEVE_TRANSLATION_PROBE_CONTAINER:-quantsieve-api}"
readonly stable_alias="translate"
readonly adopt_legacy="${QUANTSIEVE_TRANSLATION_ADOPT_LEGACY:-false}"
readonly image="${QUANTSIEVE_TRANSLATION_IMAGE:-docker.m.daocloud.io/libretranslate/libretranslate:v1.9.6@sha256:1de2d7056bb8ad607a412f4563d9abe324ff632b43b5be9428bcc8e213aebb32}"
readonly model_url="${QUANTSIEVE_TRANSLATION_MODEL_URL:-https://argos-net.com/v1/translate-en_zh-1_9.argosmodel}"
readonly model_sha256="${QUANTSIEVE_TRANSLATION_MODEL_SHA256:-433e7c4f034d87fbe2353161e05f18646d7999452f801a4e1f0378522b9850ab}"

require_boolean "QUANTSIEVE_TRANSLATION_ADOPT_LEGACY" "$adopt_legacy"
[[ "$network" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] \
  || die "translation network name is invalid"
[[ "$container_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] \
  || die "translation container name is invalid"
[[ "$probe_container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] \
  || die "translation probe container name is invalid"
[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] \
  || die "translation image must be pinned by a lowercase sha256 digest"
[[ "$model_url" == https://* ]] \
  || die "translation model URL must use HTTPS"
[[ "$model_sha256" =~ ^[0-9a-f]{64}$ ]] \
  || die "translation model sha256 must contain 64 lowercase hexadecimal characters"

require_safe_absolute_path "QUANTSIEVE_ROOT_DIR" "$root_dir_input"
require_safe_absolute_path "QUANTSIEVE_LOCK_FILE" "$lock_file_input"
require_safe_absolute_path \
  "QUANTSIEVE_TRANSLATION_MODEL_DIR" "$model_dir_input"
[[ "$root_dir_input" != "/" ]] || die "QuantSieve root directory must not be /"
reject_symlink_components "QUANTSIEVE_ROOT_DIR" "$root_dir_input"
mkdir -p -- "$root_dir_input"
reject_symlink_components "QUANTSIEVE_ROOT_DIR" "$root_dir_input"
root_dir="$(realpath -e -- "$root_dir_input")" \
  || die "could not resolve QuantSieve root directory"
readonly root_dir
[[ "$root_dir" != "/" ]] || die "resolved QuantSieve root directory must not be /"

lock_file="$(realpath -m -- "$lock_file_input")" \
  || die "could not resolve QuantSieve lock path"
readonly lock_file
case "$lock_file" in
  "$root_dir"/*) ;;
  *) die "QuantSieve lock file must stay inside the QuantSieve root directory" ;;
esac
[[ ! -L "$lock_file" ]] || die "QuantSieve lock file must not be a symbolic link"
exec 8>"$lock_file"
flock -n 8 || die "another QuantSieve build or deployment is already running"

model_dir_planned="$(realpath -m -- "$model_dir_input")" \
  || die "could not resolve translation model directory"
case "$model_dir_planned" in
  "$root_dir"/*) ;;
  *) die "translation model directory must stay inside the QuantSieve root directory" ;;
esac
reject_symlink_components \
  "QUANTSIEVE_TRANSLATION_MODEL_DIR" "$model_dir_input"
mkdir -p -- "$model_dir_planned"
reject_symlink_components \
  "QUANTSIEVE_TRANSLATION_MODEL_DIR" "$model_dir_planned"
model_dir="$(realpath -e -- "$model_dir_planned")" \
  || die "could not resolve translation model directory after creation"
readonly model_dir
case "$model_dir" in
  "$root_dir"/*) ;;
  *) die "resolved translation model directory escaped the QuantSieve root" ;;
esac

readonly model_file="$model_dir/translate-en_zh-1_9.argosmodel"
readonly installed_metadata="$model_dir/share/argos-translate/packages/translate-en_zh-1_9/metadata.json"
readonly deployment_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly deployment_id="${deployment_timestamp}-$$"
readonly candidate_name="${container_name}-candidate-${deployment_id}"
readonly previous_name="${container_name}-previous-${deployment_id}"

temporary_model=""
candidate_container_id=""
old_container_id=""
probe_container_id=""
target_image_id=""
old_container_exists=0
old_container_was_running="false"
candidate_created=0
commit_started=0
deployment_succeeded=0

trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker info >/dev/null
docker network inspect "$network" >/dev/null
assert_container_name_absent "$candidate_name"
assert_container_name_absent "$previous_name"

probe_container_id="$(
  docker container inspect --format '{{.Id}}' "$probe_container" 2>/dev/null
)" || die "translation probe container does not exist: $probe_container"
[[ "$probe_container_id" =~ ^[0-9a-f]{64}$ ]] \
  || die "could not record translation probe container identity"
container_is_running "$probe_container_id" \
  || die "translation probe container is not running: $probe_container"
container_is_on_network "$probe_container_id" \
  || die "translation probe container is not attached to $network"

if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker pull "$image"
fi
target_image_id="$(docker image inspect --format '{{.Id}}' "$image")"
readonly target_image_id
[[ "$target_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "could not record pinned translation image identity"

if old_container_id="$(
  docker container inspect --format '{{.Id}}' "$container_name" 2>/dev/null
)"
then
  old_container_exists=1
  [[ "$old_container_id" =~ ^[0-9a-f]{64}$ ]] \
    || die "could not record existing canonical container identity"
  old_container_was_running="$(
    docker container inspect --format '{{.State.Running}}' "$old_container_id"
  )"
  [[ "$old_container_was_running" =~ ^(true|false)$ ]] \
    || die "could not record existing canonical container run state"
  validate_existing_canonical_container
fi
assert_stable_alias_exclusive "$old_container_id"

readonly translator_uid="$(docker run --rm --entrypoint id "$image" -u)"
readonly translator_gid="$(docker run --rm --entrypoint id "$image" -g)"
[[ "$translator_uid" =~ ^[0-9]+$ && "$translator_gid" =~ ^[0-9]+$ ]] \
  || die "could not determine the translation image UID/GID"
chown "$translator_uid:$translator_gid" -- "$model_dir"

[[ ! -L "$model_file" ]] \
  || die "translation model archive must not be a symbolic link"
if [[ ! -f "$model_file" ]] \
  || [[ "$(sha256sum "$model_file" | awk '{print $1}')" != "$model_sha256" ]]
then
  temporary_model="$(mktemp "$model_dir/.translation-model.XXXXXX")"
  curl \
    --fail \
    --location \
    --proto '=https' \
    --proto-redir '=https' \
    --retry 3 \
    --retry-delay 2 \
    --connect-timeout 15 \
    --max-time 300 \
    --output "$temporary_model" \
    "$model_url"
  [[ "$(sha256sum "$temporary_model" | awk '{print $1}')" == "$model_sha256" ]] \
    || die "downloaded translation model failed sha256 verification"
  chown "$translator_uid:$translator_gid" -- "$temporary_model"
  chmod 600 "$temporary_model"
  mv -f -- "$temporary_model" "$model_file"
  temporary_model=""
fi
chown "$translator_uid:$translator_gid" -- "$model_file"
chmod 600 "$model_file"

reject_symlink_components "installed translation package" "$installed_metadata"
if [[ ! -f "$installed_metadata" ]]; then
  docker run \
    --rm \
    --user "$translator_uid:$translator_gid" \
    --entrypoint /app/venv/bin/python \
    --mount "type=bind,src=$model_dir,dst=/home/libretranslate/.local" \
    "$image" \
    -c \
    'from pathlib import Path
from argostranslate.package import install_from_path
install_from_path(Path("/home/libretranslate/.local/translate-en_zh-1_9.argosmodel"))'
fi
reject_symlink_components "installed translation package" "$installed_metadata"
[[ -f "$installed_metadata" ]] || die "English-to-Chinese model was not installed"

readonly health_command="/app/venv/bin/python -c 'import json, urllib.request; languages=json.load(urllib.request.urlopen(\"http://127.0.0.1:5000/languages\", timeout=3)); assert any(item.get(\"code\") == \"en\" and \"zh-Hans\" in item.get(\"targets\", []) for item in languages)'"

candidate_create_reported_success=0
if docker create \
  --name "$candidate_name" \
  --network "$network" \
  --restart no \
  --label com.quantsieve.role=translation \
  --label com.quantsieve.managed-by=nas-translation \
  --label "com.quantsieve.deployment=$deployment_id" \
  --label "com.quantsieve.model-sha256=$model_sha256" \
  --memory 3g \
  --cpus 2 \
  --pids-limit 256 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  --health-cmd "$health_command" \
  --health-interval 15s \
  --health-timeout 5s \
  --health-retries 8 \
  --health-start-period 60s \
  --env LT_HOST=0.0.0.0 \
  --env LT_LOAD_ONLY=en,zh \
  --env LT_DISABLE_WEB_UI=true \
  --env LT_DISABLE_FILES_TRANSLATION=true \
  --env LT_CHAR_LIMIT=24000 \
  --env LT_BATCH_LIMIT=8 \
  --env LT_REQ_LIMIT=30 \
  --env LT_THREADS=2 \
  --mount "type=bind,src=$model_dir,dst=/home/libretranslate/.local" \
  "$image" >/dev/null
then
  candidate_create_reported_success=1
fi
candidate_container_id="$(
  docker container inspect --format '{{.Id}}' "$candidate_name" 2>/dev/null || true
)"
if [[ -n "$candidate_container_id" ]]; then
  candidate_created=1
fi
[[ "$candidate_container_id" =~ ^[0-9a-f]{64}$ ]] \
  || die "could not record candidate translation container identity"
(( candidate_create_reported_success )) \
  || die "Docker reported failure after creating the candidate container"

docker start "$candidate_container_id" >/dev/null
wait_for_languages "$candidate_container_id"
smoke_translation_in_container "$candidate_container_id"
smoke_translation_from_probe "http://$candidate_name:5000"
assert_stable_alias_exclusive "$old_container_id"

commit_started=1
if (( old_container_exists )); then
  disconnect_container_from_network "$old_container_id"
  if container_is_running "$old_container_id"; then
    docker stop --time 30 "$old_container_id" >/dev/null
  fi
  docker rename "$old_container_id" "$previous_name"
fi

disconnect_container_from_network "$candidate_container_id"
docker network connect \
  --alias "$stable_alias" "$network" "$candidate_container_id"
assert_stable_alias_exclusive "$candidate_container_id"
docker rename "$candidate_container_id" "$container_name"
docker update --restart unless-stopped "$candidate_container_id" >/dev/null
smoke_translation_from_probe "http://$stable_alias:5000"

deployment_succeeded=1
echo "nas-translation: offline English-to-Chinese service is ready"
echo "  container: $container_name"
echo "  image: $image"
echo "  model sha256: $model_sha256"
if (( old_container_exists )); then
  echo "  previous: $previous_name (stopped, retained)"
fi
