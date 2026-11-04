"""Normalize a downloaded image without trusting archive paths or sizes."""
import os
from pathlib import Path
import shutil
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import zipfile


def zip_url(url):
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key != 'format']
    return urlunsplit(parsed._replace(query=urlencode(query + [('format', 'zip')])))


def normalize(path, expected_size):
    """Keep legacy RAW; verify a single ZIP member before replacing the input."""
    path = Path(path)
    with path.open('rb') as stream:
        zipped = stream.read(4) == b'PK\x03\x04'
    if not zipped:
        return
    temporary = path.with_suffix('.extract')
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                raise ValueError('Expected one RAW image')
            member = entries[0]
            if (member.is_dir() or '/' in member.filename or '\\' in member.filename
                    or not member.filename.endswith('.raw')
                    or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or member.flag_bits & 1 or expected_size <= 0
                    or member.file_size != expected_size):
                raise ValueError('Invalid image archive')
            with archive.open(member) as source, temporary.open('wb') as destination:
                remaining = expected_size
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('Truncated image archive')
                    destination.write(block)
                    remaining -= len(block)
                if source.read(1):
                    raise ValueError('Image exceeds declared size')
                destination.flush()
                os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
