#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_script="$script_dir/../nas-deploy.sh"
[[ -f "$deploy_script" ]] || {
  echo "missing deployment script: $deploy_script" >&2
  exit 1
}

# Load only the database restore function. Sourcing nas-deploy.sh itself would
# execute a deployment, which this fault-injection regression must never do.
restore_function="$(
  awk '
    /^restore_production_database\(\) \{$/ { capture = 1 }
    /^stop_original_container\(\) \{$/ { exit }
    capture { print }
  ' "$deploy_script"
)"
[[ "$restore_function" == *"quiesce_new_production_containers"* ]] || {
  echo "restore function has no final writer probe" >&2
  exit 1
}
[[ "$restore_function" == *'rm -f -- "${database_path}-wal"'* ]] || {
  echo "restore function extraction is incomplete" >&2
  exit 1
}
eval "$restore_function"

test_dir="$(mktemp -d)"
trap 'command rm -rf -- "$test_dir"' EXIT

backup_path="$test_dir/pre-deployment.db"
database_dir="$test_dir"
database_path="$test_dir/production.db"
restore_temp_path=""
deployment_id="writer-probe-test"
database_uid="$(id -u)"
database_gid="$(id -g)"
database_mode="600"
new_api_quiesced=1
new_container_names_clear=1

printf 'verified backup\n' >"$backup_path"
printf 'production before rollback\n' >"$database_path"
cp -- "$database_path" "$test_dir/expected-production.db"

sqlite3() {
  printf 'ok\n'
}

chown() {
  return 0
}

chmod() {
  return 0
}

database_mutation_attempted=0
rm() {
  database_mutation_attempted=1
  command rm "$@"
}

mv() {
  database_mutation_attempted=1
  command mv "$@"
}

# Model the outer rollback probe having passed, then an API writer becoming
# active while the restore temp file is prepared. The in-function final probe
# must observe the changed state and abort before WAL removal or atomic mv.
writer_running=1
final_probe_calls=0
quiesce_new_production_containers() {
  (( final_probe_calls += 1 ))
  if (( writer_running )); then
    new_api_quiesced=0
  else
    new_api_quiesced=1
  fi
}

if restore_production_database; then
  echo "restore unexpectedly succeeded with a restarted API writer" >&2
  exit 1
fi
(( final_probe_calls == 1 )) || {
  echo "expected exactly one final writer probe" >&2
  exit 1
}
(( database_mutation_attempted == 0 )) || {
  echo "database mutation occurred after writer state changed" >&2
  exit 1
}
cmp -- "$database_path" "$test_dir/expected-production.db" || {
  echo "production database changed despite the final probe failure" >&2
  exit 1
}

echo "nas-deploy final writer probe fault test: ok"
