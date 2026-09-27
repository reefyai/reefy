#!/usr/bin/env bash
# Run a workflow script with normal fail-fast semantics and a second log copy.
set -euo pipefail
script=$1
# Every standalone kernel/module step needs the same tools as Buildroot.
# Do not require pahole here: the initial Buildroot step builds it first.
if [ -n "${BR_OUTPUT:-}" ]; then
    export PATH="$BR_OUTPUT/host/bin:$BR_OUTPUT/host/sbin:$PATH"
fi
log_dir="${RUNNER_TEMP:?}/reefy-build-logs/${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}"
mkdir -p "$log_dir"
log_file="$log_dir/$(basename "$script").log"
# Never enable xtrace or dump the environment: artifact logs are not subject
# to Actions' console secret masking. These steps must only print build output.
bash --noprofile --norc -eo pipefail "$script" 2>&1 | tee "$log_file"
