#!/usr/bin/env bash
set -euo pipefail
make O="$BR_OUTPUT" -j$(nproc)
