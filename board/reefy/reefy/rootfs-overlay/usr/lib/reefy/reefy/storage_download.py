"""Admitted, atomic app-file downloads: incomplete models never become seeds."""
from contextlib import nullcontext
import os
from pathlib import Path
import re
import subprocess
import tempfile

from reefy.storage_admission import reservation
from reefy.storage_pressure import PressureError, QUANTUM
from reefy.storage_quota import Registry


def download(volume, destination, url):
    registry = Registry()
    admission = nullcontext()
    if registry.data.get('active'):
        record = next(((key, value) for key, value in registry.data['projects'].items()
                       if value['path'] == volume and value.get('complete')
                       and not value.get('retired')), None)
        if record is None:
            raise PressureError('download destination is not governed')
        # A public CDN normally supplies the final Content-Length. Some signed
        # GET URLs reject HEAD; those get bounded initial runway and still fail
        # locally at their project quota if the body is too large.
        length = 512 * 1024**2
        try:
            headers = subprocess.run(['curl', '-fsSLI', '--max-time', '20', url],
                                     capture_output=True, text=True, check=True, timeout=25).stdout
            final = re.split(r'\r?\n\r?\n', headers.strip())[-1]
            values = re.findall(r'^content-length:\s*(\d+)\s*$', final, re.M | re.I)
            if values:
                length = int(values[-1])
        except (subprocess.SubprocessError, OSError):
            pass
        budget = ((length + 64 * 1024**2 + QUANTUM - 1) // QUANTUM) * QUANTUM
        admission = reservation('app-file-download', budget,
                                storage_class=record[1]['storage_class'], target=record[0])
    temporary = None
    with admission:
        try:
            fd, temporary = tempfile.mkstemp(prefix='.reefy-download-', dir=os.path.dirname(destination))
            os.close(fd)
            subprocess.run(['curl', '-fSL', '-o', temporary, url],
                           capture_output=True, timeout=600, check=True)
            # Match the former curl-created file's readable seed permissions;
            # mkstemp's private mode would break non-root app users.
            os.chmod(temporary, 0o644)
            with open(temporary, 'rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            temporary = None
            directory = os.open(os.path.dirname(destination), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
