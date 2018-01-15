# This file is part of cloud-init. See LICENSE file for license information.

from cloudinit.config.cc_resizefs import (
    can_skip_resize, handle, is_device_path_writable_block,
    rootdev_from_cmdline)

import logging
import textwrap

from cloudinit.tests.helpers import (CiTestCase, mock, skipIf, util,
                                     wrap_and_call)


LOG = logging.getLogger(__name__)


try:
    import jsonschema
    assert jsonschema  # avoid pyflakes error F401: import unused
    _missing_jsonschema_dep = False
except ImportError:
    _missing_jsonschema_dep = True


class TestResizefs(CiTestCase):
    with_logs = True

    def setUp(self):
        super(TestResizefs, self).setUp()
        self.name = "resizefs"

    @mock.patch('cloudinit.config.cc_resizefs._get_dumpfs_output')
    @mock.patch('cloudinit.config.cc_resizefs._get_gpart_output')
    def test_skip_ufs_resize(self, gpart_out, dumpfs_out):
        fs_type = "ufs"
        resize_what = "/"
        devpth = "/dev/da0p2"
        dumpfs_out.return_value = (
            "# newfs command for / (/dev/label/rootfs)\n"
            "newfs -O 2 -U -a 4 -b 32768 -d 32768 -e 4096 "
            "-f 4096 -g 16384 -h 64 -i 8192 -j -k 6408 -m 8 "
            "-o time -s 58719232 /dev/label/rootfs\n")
        gpart_out.return_value = textwrap.dedent("""\
            =>      40  62914480  da0  GPT  (30G)
                    40      1024    1  freebsd-boot  (512K)
                  1064  58719232    2  freebsd-ufs  (28G)
              58720296   3145728    3  freebsd-swap  (1.5G)
              61866024   1048496       - free -  (512M)
            """)
        res = can_skip_resize(fs_type, resize_what, devpth)
        self.assertTrue(res)

    @mock.patch('cloudinit.config.cc_resizefs._get_dumpfs_output')
    @mock.patch('cloudinit.config.cc_resizefs._get_gpart_output')
    def test_skip_ufs_resize_roundup(self, gpart_out, dumpfs_out):
        fs_type = "ufs"
        resize_what = "/"
        devpth = "/dev/da0p2"
        dumpfs_out.return_value = (
            "# newfs command for / (/dev/label/rootfs)\n"
            "newfs -O 2 -U -a 4 -b 32768 -d 32768 -e 4096 "
            "-f 4096 -g 16384 -h 64 -i 8192 -j -k 368 -m 8 "
            "-o time -s 297080 /dev/label/rootfs\n")
        gpart_out.return_value = textwrap.dedent("""\
            =>      34  297086  da0  GPT  (145M)
                    34  297086    1  freebsd-ufs  (145M)
            """)
        res = can_skip_resize(fs_type, resize_what, devpth)
        self.assertTrue(res)

    def test_handle_noops_on_disabled(self):
        """The handle function logs when the configuration disables resize."""
        cfg = {'resize_rootfs': False}
        handle('cc_resizefs', cfg, _cloud=None, log=LOG, args=[])
        self.assertIn(
            'DEBUG: Skipping module named cc_resizefs, resizing disabled\n',
            self.logs.getvalue())

    @skipIf(_missing_jsonschema_dep, "No python-jsonschema dependency")
    def test_handle_schema_validation_logs_invalid_resize_rootfs_value(self):
        """The handle reports json schema violations as a warning.

        Invalid values for resize_rootfs result in disabling the module.
        """
        cfg = {'resize_rootfs': 'junk'}
        handle('cc_resizefs', cfg, _cloud=None, log=LOG, args=[])
        logs = self.logs.getvalue()
        self.assertIn(
            "WARNING: Invalid config:\nresize_rootfs: 'junk' is not one of"
            " [True, False, 'noblock']",
            logs)
        self.assertIn(
            'DEBUG: Skipping module named cc_resizefs, resizing disabled\n',
            logs)

    @mock.patch('cloudinit.config.cc_resizefs.util.get_mount_info')
    def test_handle_warns_on_unknown_mount_info(self, m_get_mount_info):
        """handle warns when get_mount_info sees unknown filesystem for /."""
        m_get_mount_info.return_value = None
        cfg = {'resize_rootfs': True}
        handle('cc_resizefs', cfg, _cloud=None, log=LOG, args=[])
        logs = self.logs.getvalue()
        self.assertNotIn("WARNING: Invalid config:\nresize_rootfs:", logs)
        self.assertIn(
            'WARNING: Could not determine filesystem type of /\n',
            logs)
        self.assertEqual(
            [mock.call('/', LOG)],
            m_get_mount_info.call_args_list)

    def test_handle_warns_on_undiscoverable_root_path_in_commandline(self):
        """handle noops when the root path is not found on the commandline."""
        cfg = {'resize_rootfs': True}
        exists_mock_path = 'cloudinit.config.cc_resizefs.os.path.exists'

        def fake_mount_info(path, log):
            self.assertEqual('/', path)
            self.assertEqual(LOG, log)
            return ('/dev/root', 'ext4', '/')

        with mock.patch(exists_mock_path) as m_exists:
            m_exists.return_value = False
            wrap_and_call(
                'cloudinit.config.cc_resizefs.util',
                {'is_container': {'return_value': False},
                 'get_mount_info': {'side_effect': fake_mount_info},
                 'get_cmdline': {'return_value': 'BOOT_IMAGE=/vmlinuz.efi'}},
                handle, 'cc_resizefs', cfg, _cloud=None, log=LOG,
                args=[])
        logs = self.logs.getvalue()
        self.assertIn("WARNING: Unable to find device '/dev/root'", logs)


class TestRootDevFromCmdline(CiTestCase):

    def test_rootdev_from_cmdline_with_no_root(self):
        """Return None from rootdev_from_cmdline when root is not present."""
        invalid_cases = [
            'BOOT_IMAGE=/adsf asdfa werasef  root adf', 'BOOT_IMAGE=/adsf', '']
        for case in invalid_cases:
            self.assertIsNone(rootdev_from_cmdline(case))

    def test_rootdev_from_cmdline_with_root_startswith_dev(self):
        """Return the cmdline root when the path starts with /dev."""
        self.assertEqual(
            '/dev/this', rootdev_from_cmdline('asdf root=/dev/this'))

    def test_rootdev_from_cmdline_with_root_without_dev_prefix(self):
        """Add /dev prefix to cmdline root when the path lacks the prefix."""
        self.assertEqual('/dev/this', rootdev_from_cmdline('asdf root=this'))

    def test_rootdev_from_cmdline_with_root_with_label(self):
        """When cmdline root contains a LABEL, our root is disk/by-label."""
        self.assertEqual(
            '/dev/disk/by-label/unique',
            rootdev_from_cmdline('asdf root=LABEL=unique'))

    def test_rootdev_from_cmdline_with_root_with_uuid(self):
        """When cmdline root contains a UUID, our root is disk/by-uuid."""
        self.assertEqual(
            '/dev/disk/by-uuid/adsfdsaf-adsf',
            rootdev_from_cmdline('asdf root=UUID=adsfdsaf-adsf'))


class TestIsDevicePathWritableBlock(CiTestCase):

    with_logs = True

    def test_is_device_path_writable_block_false_on_overlayroot(self):
        """When devpath is overlayroot (on MAAS), is_dev_writable is False."""
        info = 'does not matter'
        is_writable = wrap_and_call(
            'cloudinit.config.cc_resizefs.util',
            {'is_container': {'return_value': False}},
            is_device_path_writable_block, 'overlayroot', info, LOG)
        self.assertFalse(is_writable)
        self.assertIn(
            "Not attempting to resize devpath 'overlayroot'",
            self.logs.getvalue())

    def test_is_device_path_writable_block_warns_missing_cmdline_root(self):
        """When root does not exist isn't in the cmdline, log warning."""
        info = 'does not matter'

        def fake_mount_info(path, log):
            self.assertEqual('/', path)
            self.assertEqual(LOG, log)
            return ('/dev/root', 'ext4', '/')

        exists_mock_path = 'cloudinit.config.cc_resizefs.os.path.exists'
        with mock.patch(exists_mock_path) as m_exists:
            m_exists.return_value = False
            is_writable = wrap_and_call(
                'cloudinit.config.cc_resizefs.util',
                {'is_container': {'return_value': False},
                 'get_mount_info': {'side_effect': fake_mount_info},
                 'get_cmdline': {'return_value': 'BOOT_IMAGE=/vmlinuz.efi'}},
                is_device_path_writable_block, '/dev/root', info, LOG)
        self.assertFalse(is_writable)
        logs = self.logs.getvalue()
        self.assertIn("WARNING: Unable to find device '/dev/root'", logs)

    def test_is_device_path_writable_block_does_not_exist(self):
        """When devpath does not exist, a warning is logged."""
        info = 'dev=/I/dont/exist mnt_point=/ path=/dev/none'
        is_writable = wrap_and_call(
            'cloudinit.config.cc_resizefs.util',
            {'is_container': {'return_value': False}},
            is_device_path_writable_block, '/I/dont/exist', info, LOG)
        self.assertFalse(is_writable)
        self.assertIn(
            "WARNING: Device '/I/dont/exist' did not exist."
            ' cannot resize: %s' % info,
            self.logs.getvalue())

    def test_is_device_path_writable_block_does_not_exist_in_container(self):
        """When devpath does not exist in a container, log a debug message."""
        info = 'dev=/I/dont/exist mnt_point=/ path=/dev/none'
        is_writable = wrap_and_call(
            'cloudinit.config.cc_resizefs.util',
            {'is_container': {'return_value': True}},
            is_device_path_writable_block, '/I/dont/exist', info, LOG)
        self.assertFalse(is_writable)
        self.assertIn(
            "DEBUG: Device '/I/dont/exist' did not exist in container."
            ' cannot resize: %s' % info,
            self.logs.getvalue())

    def test_is_device_path_writable_block_raises_oserror(self):
        """When unexpected OSError is raises by os.stat it is reraised."""
        info = 'dev=/I/dont/exist mnt_point=/ path=/dev/none'
        with self.assertRaises(OSError) as context_manager:
            wrap_and_call(
                'cloudinit.config.cc_resizefs',
                {'util.is_container': {'return_value': True},
                 'os.stat': {'side_effect': OSError('Something unexpected')}},
                is_device_path_writable_block, '/I/dont/exist', info, LOG)
        self.assertEqual(
            'Something unexpected', str(context_manager.exception))

    def test_is_device_path_writable_block_non_block(self):
        """When device is not a block device, emit warning return False."""
        fake_devpath = self.tmp_path('dev/readwrite')
        util.write_file(fake_devpath, '', mode=0o600)  # read-write
        info = 'dev=/dev/root mnt_point=/ path={0}'.format(fake_devpath)

        is_writable = wrap_and_call(
            'cloudinit.config.cc_resizefs.util',
            {'is_container': {'return_value': False}},
            is_device_path_writable_block, fake_devpath, info, LOG)
        self.assertFalse(is_writable)
        self.assertIn(
            "WARNING: device '{0}' not a block device. cannot resize".format(
                fake_devpath),
            self.logs.getvalue())

    def test_is_device_path_writable_block_non_block_on_container(self):
        """When device is non-block device in container, emit debug log."""
        fake_devpath = self.tmp_path('dev/readwrite')
        util.write_file(fake_devpath, '', mode=0o600)  # read-write
        info = 'dev=/dev/root mnt_point=/ path={0}'.format(fake_devpath)

        is_writable = wrap_and_call(
            'cloudinit.config.cc_resizefs.util',
            {'is_container': {'return_value': True}},
            is_device_path_writable_block, fake_devpath, info, LOG)
        self.assertFalse(is_writable)
        self.assertIn(
            "DEBUG: device '{0}' not a block device in container."
            ' cannot resize'.format(fake_devpath),
            self.logs.getvalue())


# vi: ts=4 expandtab
