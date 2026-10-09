#!/usr/bin/env python3
"""Keep the existing firmware sequence when Buildroot's build tree is cleaned."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import urllib.request
import zipfile

PATTERN = re.compile(r'^\d{4}\.\d{2}\.\d{2}-\d+$')


def order(value):
    date, sequence = value.rsplit('-', 1)
    return date, int(sequence)


def published_floor(token):
    api = 'https://api.github.com/repos/reefyai/reefy'
    def get(url, raw=False):
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
        # GitHub's redirect is a signed download URL; strip the bearer on it.
        class SafeRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                return urllib.request.Request(newurl)
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=30) as response:
            data = response.read()
        return data if raw else json.loads(data)
    runs = get(api + '/actions/workflows/firmware-build.yml/runs?status=success&per_page=10')['workflow_runs']
    values = []
    for run in runs:
        artifacts = get(api + f'/actions/runs/{run["id"]}/artifacts')['artifacts']
        for artifact in artifacts:
            if artifact['name'] != 'reefy-provider-catalog' or artifact['expired']:
                continue
            with zipfile.ZipFile(io.BytesIO(get(artifact['archive_download_url'], raw=True))) as archive:
                names = [x for x in archive.namelist() if x.endswith('provider-artifacts.json')]
                if len(names) != 1:
                    raise RuntimeError('invalid published firmware version catalog')
                value = json.loads(archive.read(names[0]))['image_version']
            if not PATTERN.fullmatch(value):
                raise RuntimeError('invalid published firmware version')
            values.append(value)
    return max(values, key=order) if values else None


def preserve(output, token=None):
    paths = [output / '.reefy-last-version', output / 'build/.reefy-last-version']
    values = [p.read_text().strip() for p in paths if p.exists()]
    if any(not PATTERN.fullmatch(v) for v in values):
        raise RuntimeError('invalid local firmware sequence')
    if not values and token:
        floor = published_floor(token)
        if floor:
            values.append(floor)
    if values:
        value = max(values, key=order)
        paths[0].write_text(value + '\n')
        print('Firmware sequence floor preserved')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        preserve(args.output, os.environ.get('GITHUB_TOKEN'))
    except Exception:
        raise SystemExit('Cannot safely preserve the firmware sequence; build stopped') from None
