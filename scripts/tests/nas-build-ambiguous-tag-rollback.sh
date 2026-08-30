#!/usr/bin/env bash
set -Eeuo pipefail

readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly repository_root="$(cd -- "$script_dir/../.." && pwd)"
readonly build_script="$repository_root/scripts/nas-build.sh"
readonly test_root="$(mktemp -d "${TMPDIR:-/tmp}/quantsieve-build-rollback.XXXXXX")"
readonly fake_bin="$test_root/bin"
readonly fake_state="$test_root/docker-state"
readonly fake_marker="$test_root/ambiguous-tag-return"
readonly fake_build_log="$test_root/docker-build-labels"
readonly source_root="$test_root/source"
readonly source_tar="$test_root/source.tar"
readonly build_root="$test_root/runtime"
readonly tag="rollback-test"
readonly old_api_id="sha256:1111111111111111111111111111111111111111111111111111111111111111"
readonly old_web_id="sha256:2222222222222222222222222222222222222222222222222222222222222222"

cleanup() {
  rm -rf -- "$test_root"
}
trap cleanup EXIT

mkdir -p -- \
  "$fake_bin" \
  "$source_root/apps/api" \
  "$source_root/apps/web" \
  "$build_root"
printf 'FROM scratch\n' > "$source_root/apps/api/Dockerfile"
printf 'example-runtime==1.2.3\n' > "$source_root/apps/api/constraints.txt"
printf 'FROM scratch\n' > "$source_root/apps/web/Dockerfile"
tar --create --file "$source_tar" --directory "$source_root" .
source_sha256_line="$(sha256sum "$source_tar")"
readonly source_sha256="${source_sha256_line%%[[:space:]]*}"
dependency_sha256_line="$(sha256sum "$source_root/apps/api/constraints.txt")"
readonly dependency_sha256="${dependency_sha256_line%%[[:space:]]*}"

printf '%s|%s\n%s|%s\n' \
  "quantsieve-api:$tag" "$old_api_id" \
  "quantsieve-web:$tag" "$old_web_id" \
  > "$fake_state"

cat > "$fake_bin/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -Eeuo pipefail

readonly state="${FAKE_DOCKER_STATE:?}"
readonly ambiguous_marker="${FAKE_DOCKER_AMBIGUOUS_MARKER:?}"
readonly ambiguous_destination="${FAKE_DOCKER_AMBIGUOUS_DESTINATION:?}"
readonly build_log="${FAKE_DOCKER_BUILD_LOG:?}"
readonly candidate_api_id="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
readonly candidate_web_id="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

get_id() {
  local reference="$1"
  if [[ "$reference" == sha256:* ]]; then
    printf '%s\n' "$reference"
    return 0
  fi
  awk -F '|' -v reference="$reference" '
    $1 == reference {
      print $2
      found = 1
      exit
    }
    END {
      if (!found) exit 1
    }
  ' "$state"
}

set_reference() {
  local reference="$1"
  local image_id="$2"
  local temporary_state="${state}.tmp.$$"
  awk -F '|' -v reference="$reference" '$1 != reference' "$state" \
    > "$temporary_state"
  printf '%s|%s\n' "$reference" "$image_id" >> "$temporary_state"
  mv -- "$temporary_state" "$state"
}

remove_reference() {
  local reference="$1"
  local temporary_state="${state}.tmp.$$"
  awk -F '|' -v reference="$reference" '$1 != reference' "$state" \
    > "$temporary_state"
  mv -- "$temporary_state" "$state"
}

if [[ "${1:-}" == "info" ]]; then
  exit 0
fi

if [[ "${1:-}" == "build" ]]; then
  shift
  target=""
  labels=()
  while (( $# > 0 )); do
    case "$1" in
      --tag)
        target="$2"
        shift 2
        ;;
      --label)
        labels+=("$2")
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  case "$target" in
    quantsieve-api:*)
      candidate_id="$candidate_api_id"
      set_reference "$target" "$candidate_id"
      ;;
    quantsieve-web:*)
      candidate_id="$candidate_web_id"
      set_reference "$target" "$candidate_id"
      ;;
    *) exit 64 ;;
  esac
  for label in "${labels[@]}"; do
    printf '%s|%s\n' "$target" "$label" >> "$build_log"
    printf '%s|%s\n' "$candidate_id" "$label" >> "$build_log"
  done
  exit 0
fi

if [[ "${1:-}" == "run" ]]; then
  exit 0
fi

if [[ "${1:-}" == "history" ]]; then
  reference="${5:?}"
  get_id "$reference" >/dev/null
  printf '/bin/sh -c #(nop) verified test layer\n'
  exit 0
fi

if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  shift 2
  format=""
  if [[ "${1:-}" == "--format" ]]; then
    format="$2"
    shift 2
  fi
  reference="${1:?}"
  image_id="$(get_id "$reference")" || exit 1
  case "$format" in
    "")
      printf '{}\n'
      ;;
    "{{.Id}}")
      printf '%s\n' "$image_id"
      ;;
    "{{len .RootFS.Layers}}")
      printf '5\n'
      ;;
    "{{.Config.User}}")
      case "$image_id" in
        "$candidate_api_id") printf 'quantsieve\n' ;;
        "$candidate_web_id") printf 'node\n' ;;
        *) exit 64 ;;
      esac
      ;;
    "{{.Config.WorkingDir}}")
      printf '/app\n'
      ;;
    '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
      awk -F '|' -v reference="$reference" '
        $1 == reference \
          && $2 ~ /^org[.]opencontainers[.]image[.]revision=/ {
          sub(/^[^=]*=/, "", $2)
          print $2
          found = 1
          exit
        }
        END {
          if (!found) exit 1
        }
      ' "$build_log"
      ;;
    '{{ index .Config.Labels "com.quantsieve.dependency-lock-sha256" }}')
      awk -F '|' -v reference="$reference" '
        $1 == reference \
          && $2 ~ /^com[.]quantsieve[.]dependency-lock-sha256=/ {
          sub(/^[^=]*=/, "", $2)
          print $2
          found = 1
          exit
        }
        END {
          if (!found) exit 1
        }
      ' "$build_log"
      ;;
    *)
      exit 64
      ;;
  esac
  exit 0
fi

if [[ "${1:-}" == "image" && "${2:-}" == "tag" ]]; then
  source_reference="${3:?}"
  destination_reference="${4:?}"
  source_id="$(get_id "$source_reference")"
  set_reference "$destination_reference" "$source_id"
  if [[ "$destination_reference" == "$ambiguous_destination" \
    && ! -e "$ambiguous_marker" ]]
  then
    # Model a lost daemon response: the tag mutation is durable, but the
    # Docker client reports failure to its caller.
    : > "$ambiguous_marker"
    exit 42
  fi
  exit 0
fi

if [[ "${1:-}" == "image" && "${2:-}" == "rm" ]]; then
  remove_reference "${3:?}"
  exit 0
fi

printf 'unexpected fake docker invocation:' >&2
printf ' %q' "$@" >&2
printf '\n' >&2
exit 64
FAKE_DOCKER
chmod 700 "$fake_bin/docker"

cat > "$fake_bin/flock" <<'FAKE_FLOCK'
#!/usr/bin/env bash
exit 0
FAKE_FLOCK
chmod 700 "$fake_bin/flock"

set +e
PATH="$fake_bin:$PATH" \
FAKE_DOCKER_STATE="$fake_state" \
FAKE_DOCKER_AMBIGUOUS_MARKER="$fake_marker" \
FAKE_DOCKER_AMBIGUOUS_DESTINATION="quantsieve-web:$tag" \
FAKE_DOCKER_BUILD_LOG="$fake_build_log" \
QUANTSIEVE_ROOT_DIR="$build_root" \
QUANTSIEVE_BUILD_ROOT="$build_root/build" \
bash "$build_script" "$source_tar" "$tag" "$source_sha256" \
  > "$test_root/build.log" 2>&1
build_status=$?
set -e

if (( build_status == 0 )); then
  printf 'expected the ambiguous Web tag update to fail the build\n' >&2
  cat "$test_root/build.log" >&2
  exit 1
fi
[[ -e "$fake_marker" ]] || {
  printf 'build did not reach the ambiguous formal Web tag update\n' >&2
  cat "$test_root/build.log" >&2
  exit 1
}

awk -F '|' -v expected="org.opencontainers.image.revision=$source_sha256" '
  $1 ~ /^quantsieve-api:.*-candidate-/ && $2 == expected { found = 1 }
  END { exit found ? 0 : 1 }
' "$fake_build_log" || {
  printf 'API image did not receive the verified source archive SHA-256\n' >&2
  cat "$fake_build_log" >&2
  exit 1
}
awk -F '|' \
  -v expected="com.quantsieve.dependency-lock-sha256=$dependency_sha256" '
  $1 ~ /^quantsieve-api:.*-candidate-/ && $2 == expected { found = 1 }
  END { exit found ? 0 : 1 }
' "$fake_build_log" || {
  printf 'API image did not receive the dependency lock SHA-256\n' >&2
  cat "$fake_build_log" >&2
  exit 1
}
if grep -Fq "org.opencontainers.image.revision=$tag" "$fake_build_log"; then
  printf 'Docker tag was incorrectly recorded as the source revision\n' >&2
  cat "$fake_build_log" >&2
  exit 1
fi

state_id() {
  local reference="$1"
  awk -F '|' -v reference="$reference" '
    $1 == reference {
      print $2
      found = 1
      exit
    }
    END {
      if (!found) exit 1
    }
  ' "$fake_state"
}

actual_api_id="$(state_id "quantsieve-api:$tag")"
actual_web_id="$(state_id "quantsieve-web:$tag")"
[[ "$actual_api_id" == "$old_api_id" ]] || {
  printf 'API tag was not restored: expected %s, got %s\n' \
    "$old_api_id" "$actual_api_id" >&2
  cat "$test_root/build.log" >&2
  exit 1
}
[[ "$actual_web_id" == "$old_web_id" ]] || {
  printf 'Web tag was not restored after ambiguous success: expected %s, got %s\n' \
    "$old_web_id" "$actual_web_id" >&2
  cat "$test_root/build.log" >&2
  exit 1
}

if awk -F '|' '$1 ~ /-candidate-/ { found = 1 } END { exit found ? 0 : 1 }' \
  "$fake_state"
then
  printf 'candidate tags were not cleaned after rollback\n' >&2
  cat "$fake_state" >&2
  exit 1
fi

printf 'nas-build ambiguous-success rollback test passed\n'
