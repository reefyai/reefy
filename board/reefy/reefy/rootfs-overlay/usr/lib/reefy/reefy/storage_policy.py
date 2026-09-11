"""Validate and normalize the version-one storage policy before mutations."""
import os


APP_ROOT = '/mnt/reefy-data/apps'


def storage_policy(state, *, active=False, previous=None):
    """Return path -> effective class, or None for an identified legacy state.

    Once activated, missing markers retain protection using the last validated
    map for existing volumes. Newly declared volumes default to state. Unknown
    revisions and malformed explicit values never overwrite the valid policy.
    """
    marker = state.get('storage_pressure_policy')
    if 'storage_pressure_policy' in state and (not isinstance(marker, dict)
                               or set(marker) != {'version'}
                               or type(marker['version']) is not int
                               or marker['version'] != 1):
        raise ValueError('unsupported storage pressure policy')
    apps = state.get('apps') if state.get('schema_version') == 2 else None
    groups = apps if apps is not None else [state]
    legacy = marker is None and not active
    classes, declared, caps = {}, set(), set()
    for group in groups:
        values = group.get('volume_storage_classes', {})
        if not isinstance(values, dict):
            raise ValueError('volume_storage_classes must be a map')
        if legacy:
            if values:
                raise ValueError('storage classes require policy activation')
            continue  # do not impose new ownership rules on legacy configurations
        volumes = group.get('volumes' if apps is not None else 'app_volumes') or []
        for volume in volumes:
            path = volume.get('path')
            if (not isinstance(path, str) or not path.startswith(APP_ROOT + '/')
                    or os.path.normpath(path) != path
                    or len(path[len(APP_ROOT) + 1:].split('/')) != 2):
                raise ValueError('invalid app-volume ownership path')
            declared.add(path)
        for path, value in values.items():
            if not isinstance(value, str) or value not in ('bulk', 'state'):
                raise ValueError('unknown volume storage class')
            if path in classes and classes[path] != value:
                raise ValueError('conflicting shared volume class')
            classes[path] = value
        caps.update((group.get('volume_caps') or {}).keys())
    if classes.keys() - declared:
        raise ValueError('storage class refers to an undeclared volume')
    if classes.keys() & caps:
        raise ValueError('volume cannot have both a legacy cap and storage class')
    if legacy:
        return None
    if caps:
        raise ValueError('resolve legacy volume caps before policy activation')
    previous = previous or {}
    return {path: classes.get(path, previous.get(path, 'state')) for path in sorted(declared)}
