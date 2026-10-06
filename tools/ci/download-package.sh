#!/usr/bin/env bash
# Download a read-only build input without exposing partial content to consumers.
set -euo pipefail
url=$1
destination=$2
mkdir -p "$(dirname "$destination")"
partial=$(mktemp "${destination}.partial.XXXXXX")
trap 'rm -f "$partial"' EXIT
# Default --retry excludes receive errors such as a broken TLS record (exit 56).
# This is a GET into a curl-owned output file, so retries reset partial content.
curl --fail --location --retry 3 --retry-all-errors \
  --retry-max-time 180 --connect-timeout 20 --max-time 60 \
  --output "$partial" "$url"
mv -f "$partial" "$destination"
