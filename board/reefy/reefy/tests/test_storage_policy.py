import unittest

import _bootstrap  # noqa: F401
from reefy.storage_policy import storage_policy


class PolicyTests(unittest.TestCase):
    def state(self, schema=1):
        path = '/mnt/reefy-data/apps/synthetic-instance/media'
        group = {'app_volumes': [{'path': path}], 'volume_storage_classes': {path: 'bulk'}}
        if schema == 2:
            group['volumes'] = group.pop('app_volumes')
            return {'schema_version': 2, 'storage_pressure_policy': {'version': 1}, 'apps': [group]}, path
        return dict(group, storage_pressure_policy={'version': 1}), path

    def test_schemas_resolve_same_owned_class(self):
        for schema in (1, 2):
            state, path = self.state(schema)
            self.assertEqual(storage_policy(state), {path: 'bulk'})

    def test_omission_does_not_disable_active_policy(self):
        state, path = self.state()
        state.pop('storage_pressure_policy')
        state.pop('volume_storage_classes')
        self.assertIsNone(storage_policy(state))
        self.assertEqual(storage_policy(state, active=True, previous={path: 'bulk'}), {path: 'bulk'})

    def test_explicit_invalid_values_do_not_default(self):
        for value in (None, True, 90, {}, [], 'standard', 'BULK'):
            state, path = self.state()
            state['volume_storage_classes'][path] = value
            with self.assertRaises(ValueError):
                storage_policy(state)

    def test_conflict_and_bad_revision_fail(self):
        state, path = self.state()
        state['volume_caps'] = {path: 80}
        with self.assertRaises(ValueError):
            storage_policy(state)
        state.pop('volume_caps')
        state['storage_pressure_policy'] = {'version': True}
        with self.assertRaises(ValueError):
            storage_policy(state)


if __name__ == '__main__':
    unittest.main()
