#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_script="$script_dir/../nas-deploy.sh"
[[ -f "$deploy_script" ]] || {
  echo "missing deployment script: $deploy_script" >&2
  exit 1
}

# Load only the post-start contract assertion. Sourcing nas-deploy.sh would
# execute a deployment, which this fault-injection regression must never do.
contract_function="$(
  awk '
    /^assert_api_contracts\(\) \{$/ { capture = 1 }
    /^wait_for_translation_health\(\) \{$/ { exit }
    capture { print }
  ' "$deploy_script"
)"
[[ "$contract_function" == *"return 1"* ]] || {
  echo "contract assertion does not return failure to the ERR trap" >&2
  exit 1
}
eval "$contract_function"

docker() {
  return 1
}

# An explicit `exit` inside assert_api_contracts would bypass the deployment
# ERR trap and return 1. A normal function failure must reach the trap marker.
set +e
(
  set -Ee
  trap 'exit 73' ERR
  assert_api_contracts quantsieve-api-fault-injection
)
status=$?
set -e

(( status == 73 )) || {
  echo "API contract failure bypassed the deployment ERR trap" >&2
  exit 1
}

echo "nas-deploy contract rollback fault test: ok"
