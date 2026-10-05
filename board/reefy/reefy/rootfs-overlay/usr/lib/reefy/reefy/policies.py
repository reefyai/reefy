"""Built-in host policies. Desired state selects behavior, never executable code."""

import hashlib
import json
from pathlib import Path
import re
import subprocess

from reefy.apply_results import sanitize_apply_error, apply_warning


def _supports_apst(controller):
    """Identify only when the kernel's APST QoS interface is absent."""
    if (controller / 'state').read_text().strip() != 'live':
        raise RuntimeError('controller is not live; APST capability is unavailable')
    try:
        result = subprocess.run(
            ['nvme', 'id-ctrl', '/dev/' + controller.name, '-o', 'json'],
            capture_output=True, text=True, timeout=5, check=False)
        if result.returncode != 0:
            raise ValueError('identify failed')
        apsta = json.loads(result.stdout)['apsta']
        if type(apsta) is not int or not 0 <= apsta <= 255:
            raise ValueError('invalid capability')
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        raise RuntimeError('cannot determine controller APST support') from None
    return bool(apsta & 1)


def apply_apst(value, controllers=Path('/sys/class/nvme'),
               default_latency=Path('/sys/module/nvme_core/parameters/default_ps_max_latency_us'),
               managed=Path('/run/reefy-policies/nvme-apst')):
    """Reconcile every controller; retain rollback markers until restoration succeeds.

    Markers live in RAM: a boot resets both these markers and kernel QoS overrides.
    Controller identity uses the resolved sysfs path, not enumeration order alone.
    """
    if value is not None and value not in ('disabled', 'default'):
        raise ValueError('expected disabled or default')
    errors = []
    for controller in sorted(controllers.glob('nvme*')):
        if not re.fullmatch(r'nvme[0-9]+', controller.name):
            continue
        marker = managed / hashlib.sha256(
            str(controller.resolve()).encode()).hexdigest()
        if value is None and not marker.exists():
            continue
        qos = controller / 'power/pm_qos_latency_tolerance_us'
        try:
            if not qos.exists():
                if not _supports_apst(controller):
                    # Unsupported hardware has no transition for us to disable.
                    marker.unlink(missing_ok=True)
                    continue
                raise ValueError('controller has no APST latency QoS interface')
            target = '0' if value == 'disabled' else default_latency.read_text().strip()
            if not target.isdecimal():
                raise ValueError('invalid kernel default latency')
            if value == 'disabled':
                managed.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Record ownership before writing, including a crash between operations.
                marker.touch(mode=0o600, exist_ok=True)
            if qos.read_text().strip() != target:
                qos.write_text(target + '\n')
            if qos.read_text().strip() != target:
                raise ValueError('latency QoS readback did not match')
            if value != 'disabled':
                marker.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f'{controller.name}: {sanitize_apply_error(str(exc))}')
    if errors:
        raise RuntimeError('; '.join(errors))


# Only firmware-owned handlers can run. Add new independent leaves here.
HANDLERS = {('hardware', 'nvme', 'apst'): apply_apst}


def apply_policies(policies, handlers=None):
    """Validate and isolate each policy; omission lets handlers undo owned settings."""
    handlers = HANDLERS if handlers is None else handlers
    warnings = []
    supplied = {}
    invalid = set()

    def warn(path, error):
        warnings.append(apply_warning(
            'policy.apply_failed', str(error), 'policy',
            'host.policies' + ('.' + '.'.join(path) if path else '')))


    def walk(node, path=()):
        if path in handlers:
            supplied[path] = node
            # JSON null is not an explicit policy mode.
            if node is None:
                invalid.add(path)
                warn(path, 'null is not a policy value; omit the field to remove it')
            return
        if not isinstance(node, dict):
            invalid.add(path)
            warn(path, 'expected a policy object')
            return
        for key, value in node.items():
            child = path + (key,)
            if any(p[:len(child)] == child for p in handlers):
                walk(value, child)
            else:
                # Do not echo arbitrary untrusted keys or values into status.
                warn(path, 'unsupported policy key')

    walk(policies)
    for path, handler in handlers.items():
        if any(path[:len(prefix)] == prefix for prefix in invalid):
            continue
        try:
            handler(supplied.get(path))
        except Exception as exc:
            warn(path, exc)
    return warnings
