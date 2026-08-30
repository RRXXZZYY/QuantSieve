#!/usr/bin/env bash
set -Eeuo pipefail

readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly deploy_script="$script_dir/../nas-deploy.sh"
readonly valid_image_id="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
readonly valid_source_revision="1111111111111111111111111111111111111111111111111111111111111111"
readonly valid_dependency_lock="2222222222222222222222222222222222222222222222222222222222222222"

[[ -f "$deploy_script" ]] || {
  echo "missing deployment script: $deploy_script" >&2
  exit 1
}

provenance_function="$(
  awk '
    /^read_api_image_provenance\(\) \{$/ { capture = 1 }
    /^wait_for_api_health\(\) \{$/ { exit }
    capture { print }
  ' "$deploy_script"
)"
[[ "$provenance_function" == *"org.opencontainers.image.revision"* ]] || {
  echo "provenance reader does not inspect the source revision label" >&2
  exit 1
}
[[ "$provenance_function" == *"com.quantsieve.dependency-lock-sha256"* ]] || {
  echo "provenance reader does not inspect the dependency lock label" >&2
  exit 1
}
eval "$provenance_function"

die() {
  exit 1
}

fake_source_revision="$valid_source_revision"
fake_dependency_lock="$valid_dependency_lock"
fake_inspect_failure=0

docker() {
  [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]] || return 64
  [[ "${3:-}" == "--format" && "${5:-}" == "$valid_image_id" ]] || return 64
  (( fake_inspect_failure == 0 )) || return 42

  case "${4:-}" in
    *org.opencontainers.image.revision*)
      printf '%s\n' "$fake_source_revision"
      ;;
    *com.quantsieve.dependency-lock-sha256*)
      printf '%s\n' "$fake_dependency_lock"
      ;;
    *)
      return 64
      ;;
  esac
}

api_source_revision=""
api_dependency_lock_sha256=""
web_source_revision=""
read_api_image_provenance "$valid_image_id"
[[ "$api_source_revision" == "$valid_source_revision" ]] || {
  echo "valid source revision was not loaded" >&2
  exit 1
}
[[ "$api_dependency_lock_sha256" == "$valid_dependency_lock" ]] || {
  echo "valid dependency lock was not loaded" >&2
  exit 1
}
read_web_image_provenance "$valid_image_id" "$api_source_revision"
[[ "$web_source_revision" == "$valid_source_revision" ]] || {
  echo "valid Web source revision was not loaded" >&2
  exit 1
}

assert_provenance_rejected() {
  local description="$1"
  local image_id="$2"
  local source_revision="$3"
  local dependency_lock="$4"
  local inspect_failure="$5"

  if (
    api_source_revision=""
    api_dependency_lock_sha256=""
    fake_source_revision="$source_revision"
    fake_dependency_lock="$dependency_lock"
    fake_inspect_failure="$inspect_failure"
    read_api_image_provenance "$image_id"
  ) >/dev/null 2>&1
  then
    echo "invalid provenance was accepted: $description" >&2
    exit 1
  fi
}

assert_provenance_rejected \
  "unverified image reference" \
  "quantsieve-api:release-tag" \
  "$valid_source_revision" \
  "$valid_dependency_lock" \
  0
assert_provenance_rejected \
  "missing source label" \
  "$valid_image_id" \
  "<no value>" \
  "$valid_dependency_lock" \
  0
assert_provenance_rejected \
  "uppercase source label" \
  "$valid_image_id" \
  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" \
  "$valid_dependency_lock" \
  0
assert_provenance_rejected \
  "short dependency label" \
  "$valid_image_id" \
  "$valid_source_revision" \
  "2222" \
  0
assert_provenance_rejected \
  "image inspection failure" \
  "$valid_image_id" \
  "$valid_source_revision" \
  "$valid_dependency_lock" \
  1

assert_web_provenance_rejected() {
  local description="$1"
  local source_revision="$2"
  local expected_source_revision="$3"
  local inspect_failure="$4"

  if (
    web_source_revision=""
    fake_source_revision="$source_revision"
    fake_inspect_failure="$inspect_failure"
    read_web_image_provenance "$valid_image_id" "$expected_source_revision"
  ) >/dev/null 2>&1
  then
    echo "invalid Web provenance was accepted: $description" >&2
    exit 1
  fi
}

assert_web_provenance_rejected \
  "API/Web source mismatch" \
  "3333333333333333333333333333333333333333333333333333333333333333" \
  "$valid_source_revision" \
  0
assert_web_provenance_rejected \
  "missing Web source label" \
  "<no value>" \
  "$valid_source_revision" \
  0
assert_web_provenance_rejected \
  "Web image inspection failure" \
  "$valid_source_revision" \
  "$valid_source_revision" \
  1

source_env_count="$(
  grep -F -c -- \
    '--env "QUANTSIEVE_SOURCE_REVISION=$api_source_revision" \' \
    "$deploy_script"
)"
dependency_env_count="$(
  grep -F -c -- \
    '--env "QUANTSIEVE_DEPENDENCY_LOCK_SHA256=$api_dependency_lock_sha256" \' \
    "$deploy_script"
)"
[[ "$source_env_count" == "2" && "$dependency_env_count" == "2" ]] || {
  echo "candidate and production API containers must both receive provenance" >&2
  exit 1
}

candidate_api_function="$(
  awk '
    /^create_candidate_api\(\) \{$/ { capture = 1 }
    /^create_candidate_web\(\) \{$/ { exit }
    capture { print }
  ' "$deploy_script"
)"
production_api_function="$(
  awk '
    /^create_production_api\(\) \{$/ { capture = 1 }
    /^create_production_web\(\) \{$/ { exit }
    capture { print }
  ' "$deploy_script"
)"
for api_function in "$candidate_api_function" "$production_api_function"; do
  [[ "$api_function" == *'"$new_api_image_id" >/dev/null'* ]] || {
    echo "API container is not created from the verified immutable image ID" >&2
    exit 1
  }
  [[ "$api_function" != *'"$api_image" >/dev/null'* ]] || {
    echo "API container creation still trusts a mutable image tag" >&2
    exit 1
  }
done

candidate_web_function="$(
  awk '
    /^create_candidate_web\(\) \{$/ { capture = 1 }
    /^create_production_api\(\) \{$/ { exit }
    capture { print }
  ' "$deploy_script"
)"
production_web_function="$(
  awk '
    /^create_production_web\(\) \{$/ { capture = 1 }
    /^docker info >\/dev\/null$/ { exit }
    capture { print }
  ' "$deploy_script"
)"
for web_function in "$candidate_web_function" "$production_web_function"; do
  [[ "$web_function" == *'"$new_web_image_id" >/dev/null'* ]] || {
    echo "Web container is not created from the verified immutable image ID" >&2
    exit 1
  }
  [[ "$web_function" != *'"$web_image" >/dev/null'* ]] || {
    echo "Web container creation still trusts a mutable image tag" >&2
    exit 1
  }
done

if grep -Fq \
  'QUANTSIEVE_SOURCE_REVISION=$tag' \
  "$deploy_script"
then
  echo "Docker tag is still being presented as a source revision" >&2
  exit 1
fi

echo "nas-deploy provenance test passed"
