#!/usr/bin/env bash
set -Eeuo pipefail

repository_root="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P
)"
deploy_script="$repository_root/scripts/nas-deploy.sh"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

eval "$(
  awk '
    /^deployed_version_fingerprint\(\)/ { capture = 1 }
    /^container_matches_deployment\(\)/ { capture = 0 }
    capture { print }
  ' "$deploy_script"
)"

reset_case() {
  root_dir="$1"
  tag="$2"
  deployment_id="receipt-test-$$"
  mkdir -p -- "$root_dir"
  deployed_version_path="$root_dir/DEPLOYED_VERSION"
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
}

assert_receipt() {
  local expected="$1"

  [[ "$(stat -c '%a' -- "$deployed_version_path")" == "600" ]]
  [[ "$(cat -- "$deployed_version_path")" == "$expected" ]]
}

# Successful publication is a same-directory inode replacement with mode 0600.
case_root="$test_root/success"
reset_case "$case_root" "new-tag"
printf '%s\n' "old-tag" >"$deployed_version_path"
chmod 600 "$deployed_version_path"
old_identity="$(deployed_version_identity "$deployed_version_path")"
snapshot_deployed_version
publish_deployed_version
verify_written_deployed_version
assert_receipt "new-tag"
[[ "$(deployed_version_identity "$deployed_version_path")" != "$old_identity" ]]

# A failure after publication restores the exact previous receipt.
case_root="$test_root/rollback-present"
reset_case "$case_root" "new-tag"
printf '%s\n' "old-tag" >"$deployed_version_path"
chmod 600 "$deployed_version_path"
snapshot_deployed_version
publish_deployed_version
set +e
(
  set -Ee
  trap 'trap - ERR; restore_deployed_version; exit 97' ERR
  false
)
injected_status=$?
set -e
if (( injected_status != 97 )); then
  echo "expected the injected post-publication failure" >&2
  exit 1
fi
assert_receipt "old-tag"

# If the receipt did not exist, rollback removes only this invocation's file.
case_root="$test_root/rollback-absent"
reset_case "$case_root" "new-tag"
snapshot_deployed_version
publish_deployed_version
set +e
(
  set -Ee
  trap 'trap - ERR; restore_deployed_version; exit 98' ERR
  false
)
injected_status=$?
set -e
if (( injected_status != 98 )); then
  echo "expected the injected absent-receipt failure" >&2
  exit 1
fi
[[ ! -e "$deployed_version_path" && ! -L "$deployed_version_path" ]]

# A change after the snapshot blocks publication and is not overwritten.
case_root="$test_root/conflict-before"
reset_case "$case_root" "new-tag"
printf '%s\n' "old-tag" >"$deployed_version_path"
chmod 600 "$deployed_version_path"
snapshot_deployed_version
printf '%s\n' "external-tag" >"$case_root/external"
chmod 600 "$case_root/external"
mv -f -- "$case_root/external" "$deployed_version_path"
if publish_deployed_version; then
  echo "publication unexpectedly overwrote an external change" >&2
  exit 1
fi
assert_receipt "external-tag"

# A change after publication blocks rollback and is likewise preserved.
case_root="$test_root/conflict-after"
reset_case "$case_root" "new-tag"
printf '%s\n' "old-tag" >"$deployed_version_path"
chmod 600 "$deployed_version_path"
snapshot_deployed_version
publish_deployed_version
printf '%s\n' "external-tag" >"$deployed_version_path"
if restore_deployed_version; then
  echo "rollback unexpectedly overwrote an external change" >&2
  exit 1
fi
assert_receipt "external-tag"

echo "nas-deploy DEPLOYED_VERSION receipt checks passed"
