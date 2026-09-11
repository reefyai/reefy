import contextlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from reefy.storage_firmware import squashfs_extent, require_compatible_image, image_loop
from reefy.storage_pressure import PressureError


def firmware_fixture(payload=b'hsqs' + bytes(124)):
    name = b'./rootfs.squashfs\0'
    fields = [1, 0o100644, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(name), 0]
    cpio = b'070701' + b''.join(f'{value:08x}'.encode() for value in fields) + name
    cpio += bytes(-len(cpio) % 4)
    payload_offset = len(cpio)
    cpio += payload
    image = bytearray(512)
    image[:2] = b'MZ'
    struct.pack_into('<I', image, 60, 128)
    image[128:132] = b'PE\0\0'
    struct.pack_into('<H', image, 134, 1)
    image[152:160] = b'.initrd\0'
    struct.pack_into('<II', image, 168, len(cpio), 512)
    return bytes(image) + cpio, 512 + payload_offset


class FirmwareProtectionTests(unittest.TestCase):
    def test_extent_points_at_exact_squashfs_bytes(self):
        data, offset = firmware_fixture()
        with tempfile.TemporaryFile() as stream:
            stream.write(data)
            stream.flush()
            self.assertEqual(squashfs_extent(stream), (offset, 128))

    def test_truncated_invalid_and_missing_payload_fail_closed(self):
        valid, _ = firmware_fixture()
        for data in (b'MZ', bytes(1024), valid[:-1], valid.replace(b'hsqs', b'nope'),
                     valid.replace(b'070701', b'070707'), valid.replace(b'.initrd', b'.otherx')):
            with tempfile.TemporaryFile() as stream:
                stream.write(data)
                stream.flush()
                with self.assertRaises(PressureError):
                    squashfs_extent(stream)

    def test_actual_manifest_capability_not_version_controls_admission(self):
        image, _ = firmware_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target.efi'
            target.write_bytes(image)
            manifest_path = root / 'usr/share/reefy/compatibility.json'
            manifest_path.parent.mkdir(parents=True)
            for revision in (None, 1, 2):
                features = {} if revision is None else {'storage_pressure_quotas': revision}
                manifest_path.write_text(json.dumps({'manifest_version': 1, 'protocols': {
                    'desired_state': {'versions': [1], 'features': features}}}))
                with patch('reefy.storage_firmware.Registry', return_value=Mock(data={'active': True})), \
                        patch('reefy.storage_firmware.image_loop',
                              return_value=contextlib.nullcontext('/dev/loop7')), \
                        patch('reefy.storage_firmware.readonly_mount',
                              return_value=contextlib.nullcontext(root)):
                    if revision == 1:
                        require_compatible_image(target)
                    else:
                        with self.assertRaises(PressureError):
                            require_compatible_image(target)

    def test_loop_extent_readonly_autoclear_and_exception_release(self):
        files = [Mock(), Mock(), Mock()]
        for index, stream in enumerate(files):
            stream.fileno.return_value = index + 10
        opened = []

        @contextlib.contextmanager
        def open_file(*args, **kwargs):
            stream = files[len(opened)]
            opened.append(stream)
            try:
                yield stream
            finally:
                stream.close()

        def ioctl(fd, operation, config=None):
            if operation == 0x4C82:
                return 7
            self.assertEqual((fd, operation, len(config)), (12, 0x4C0A, 304))
            self.assertEqual(struct.unpack_from('=I', config, 0)[0], 10)
            self.assertEqual(struct.unpack_from('=QQ', config, 32), (1234, 8192))
            self.assertEqual(struct.unpack_from('=I', config, 60)[0], 5)
            return 0

        with patch('reefy.storage_firmware.open', side_effect=open_file), \
                patch('reefy.storage_firmware.fcntl.ioctl', side_effect=ioctl):
            with self.assertRaisesRegex(RuntimeError, 'mount failed'):
                with image_loop('/target.efi', 1234, 8192) as device:
                    self.assertEqual(device, '/dev/loop7')
                    self.assertFalse(files[-1].close.called)
                    raise RuntimeError('mount failed')
        for stream in files:
            stream.close.assert_called_once()

    def test_legacy_device_does_not_require_new_target_capability(self):
        with patch('reefy.storage_firmware.Registry', return_value=Mock(data={})), \
                patch('reefy.storage_firmware.squashfs_extent') as inspect:
            require_compatible_image('/nonexistent-legacy-target')
            inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
