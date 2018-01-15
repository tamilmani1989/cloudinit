# Copyright (C) 2016 Canonical Ltd.
#
# Author: Scott Moser <scott.moser@canonical.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import base64
from collections import OrderedDict

from cloudinit.tests import helpers as test_helpers

from cloudinit.sources import DataSourceOVF as dsovf

OVF_ENV_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<Environment xmlns="http://schemas.dmtf.org/ovf/environment/1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:oe="http://schemas.dmtf.org/ovf/environment/1"
  xsi:schemaLocation="http://schemas.dmtf.org/ovf/environment/1 ../dsp8027.xsd"
  oe:id="WebTier">
  <!-- Information about hypervisor platform -->
  <oe:PlatformSection>
      <Kind>ESX Server</Kind>
      <Version>3.0.1</Version>
      <Vendor>VMware, Inc.</Vendor>
      <Locale>en_US</Locale>
  </oe:PlatformSection>
  <!--- Properties defined for this virtual machine -->
  <PropertySection>
{properties}
  </PropertySection>
</Environment>
"""


def fill_properties(props, template=OVF_ENV_CONTENT):
    lines = []
    prop_tmpl = '<Property oe:key="{key}" oe:value="{val}"/>'
    for key, val in props.items():
        lines.append(prop_tmpl.format(key=key, val=val))
    indent = "        "
    properties = ''.join([indent + l + "\n" for l in lines])
    return template.format(properties=properties)


class TestReadOvfEnv(test_helpers.TestCase):
    def test_with_b64_userdata(self):
        user_data = "#!/bin/sh\necho hello world\n"
        user_data_b64 = base64.b64encode(user_data.encode()).decode()
        props = {"user-data": user_data_b64, "password": "passw0rd",
                 "instance-id": "inst-001"}
        env = fill_properties(props)
        md, ud, cfg = dsovf.read_ovf_environment(env)
        self.assertEqual({"instance-id": "inst-001"}, md)
        self.assertEqual(user_data.encode(), ud)
        self.assertEqual({'password': "passw0rd"}, cfg)

    def test_with_non_b64_userdata(self):
        user_data = "my-user-data"
        props = {"user-data": user_data, "instance-id": "inst-001"}
        env = fill_properties(props)
        md, ud, cfg = dsovf.read_ovf_environment(env)
        self.assertEqual({"instance-id": "inst-001"}, md)
        self.assertEqual(user_data.encode(), ud)
        self.assertEqual({}, cfg)

    def test_with_no_userdata(self):
        props = {"password": "passw0rd", "instance-id": "inst-001"}
        env = fill_properties(props)
        md, ud, cfg = dsovf.read_ovf_environment(env)
        self.assertEqual({"instance-id": "inst-001"}, md)
        self.assertEqual({'password': "passw0rd"}, cfg)
        self.assertIsNone(ud)


class TestTransportIso9660(test_helpers.CiTestCase):

    def setUp(self):
        super(TestTransportIso9660, self).setUp()
        self.add_patch('cloudinit.util.find_devs_with',
                       'm_find_devs_with')
        self.add_patch('cloudinit.util.mounts', 'm_mounts')
        self.add_patch('cloudinit.util.mount_cb', 'm_mount_cb')
        self.add_patch('cloudinit.sources.DataSourceOVF.get_ovf_env',
                       'm_get_ovf_env')
        self.m_get_ovf_env.return_value = ('myfile', 'mycontent')

    def test_find_already_mounted(self):
        """Check we call get_ovf_env from on matching mounted devices"""
        mounts = {
            '/dev/sr9': {
                'fstype': 'iso9660',
                'mountpoint': 'wark/media/sr9',
                'opts': 'ro',
            }
        }
        self.m_mounts.return_value = mounts

        (contents, fullp, fname) = dsovf.transport_iso9660()
        self.assertEqual("mycontent", contents)
        self.assertEqual("/dev/sr9", fullp)
        self.assertEqual("myfile", fname)

    def test_find_already_mounted_skips_non_iso9660(self):
        """Check we call get_ovf_env ignoring non iso9660"""
        mounts = {
            '/dev/xvdb': {
                'fstype': 'vfat',
                'mountpoint': 'wark/foobar',
                'opts': 'defaults,noatime',
            },
            '/dev/xvdc': {
                'fstype': 'iso9660',
                'mountpoint': 'wark/media/sr9',
                'opts': 'ro',
            }
        }
        # We use an OrderedDict here to ensure we check xvdb before xvdc
        # as we're not mocking the regex matching, however, if we place
        # an entry in the results then we can be reasonably sure that
        # we're skipping an entry which fails to match.
        self.m_mounts.return_value = (
            OrderedDict(sorted(mounts.items(), key=lambda t: t[0])))

        (contents, fullp, fname) = dsovf.transport_iso9660()
        self.assertEqual("mycontent", contents)
        self.assertEqual("/dev/xvdc", fullp)
        self.assertEqual("myfile", fname)

    def test_find_already_mounted_matches_kname(self):
        """Check we dont regex match on basename of the device"""
        mounts = {
            '/dev/foo/bar/xvdc': {
                'fstype': 'iso9660',
                'mountpoint': 'wark/media/sr9',
                'opts': 'ro',
            }
        }
        # we're skipping an entry which fails to match.
        self.m_mounts.return_value = mounts

        (contents, fullp, fname) = dsovf.transport_iso9660()
        self.assertEqual(False, contents)
        self.assertIsNone(fullp)
        self.assertIsNone(fname)

    def test_mount_cb_called_on_blkdevs_with_iso9660(self):
        """Check we call mount_cb on blockdevs with iso9660 only"""
        self.m_mounts.return_value = {}
        self.m_find_devs_with.return_value = ['/dev/sr0']
        self.m_mount_cb.return_value = ("myfile", "mycontent")

        (contents, fullp, fname) = dsovf.transport_iso9660()

        self.m_mount_cb.assert_called_with(
            "/dev/sr0", dsovf.get_ovf_env, mtype="iso9660")
        self.assertEqual("mycontent", contents)
        self.assertEqual("/dev/sr0", fullp)
        self.assertEqual("myfile", fname)

    def test_mount_cb_called_on_blkdevs_with_iso9660_check_regex(self):
        """Check we call mount_cb on blockdevs with iso9660 and match regex"""
        self.m_mounts.return_value = {}
        self.m_find_devs_with.return_value = [
            '/dev/abc', '/dev/my-cdrom', '/dev/sr0']
        self.m_mount_cb.return_value = ("myfile", "mycontent")

        (contents, fullp, fname) = dsovf.transport_iso9660()

        self.m_mount_cb.assert_called_with(
            "/dev/sr0", dsovf.get_ovf_env, mtype="iso9660")
        self.assertEqual("mycontent", contents)
        self.assertEqual("/dev/sr0", fullp)
        self.assertEqual("myfile", fname)

    def test_mount_cb_not_called_no_matches(self):
        """Check we don't call mount_cb if nothing matches"""
        self.m_mounts.return_value = {}
        self.m_find_devs_with.return_value = ['/dev/vg/myovf']

        (contents, fullp, fname) = dsovf.transport_iso9660()

        self.assertEqual(0, self.m_mount_cb.call_count)
        self.assertEqual(False, contents)
        self.assertIsNone(fullp)
        self.assertIsNone(fname)

    def test_mount_cb_called_require_iso_false(self):
        """Check we call mount_cb on blockdevs with require_iso=False"""
        self.m_mounts.return_value = {}
        self.m_find_devs_with.return_value = ['/dev/xvdz']
        self.m_mount_cb.return_value = ("myfile", "mycontent")

        (contents, fullp, fname) = dsovf.transport_iso9660(require_iso=False)

        self.m_mount_cb.assert_called_with(
            "/dev/xvdz", dsovf.get_ovf_env, mtype=None)
        self.assertEqual("mycontent", contents)
        self.assertEqual("/dev/xvdz", fullp)
        self.assertEqual("myfile", fname)

    def test_maybe_cdrom_device_none(self):
        """Test maybe_cdrom_device returns False for none/empty input"""
        self.assertFalse(dsovf.maybe_cdrom_device(None))
        self.assertFalse(dsovf.maybe_cdrom_device(''))

    def test_maybe_cdrom_device_non_string_exception(self):
        """Test maybe_cdrom_device raises ValueError on non-string types"""
        with self.assertRaises(ValueError):
            dsovf.maybe_cdrom_device({'a': 'eleven'})

    def test_maybe_cdrom_device_false_on_multi_dir_paths(self):
        """Test maybe_cdrom_device is false on /dev[/.*]/* paths"""
        self.assertFalse(dsovf.maybe_cdrom_device('/dev/foo/sr0'))
        self.assertFalse(dsovf.maybe_cdrom_device('foo/sr0'))
        self.assertFalse(dsovf.maybe_cdrom_device('../foo/sr0'))
        self.assertFalse(dsovf.maybe_cdrom_device('../foo/sr0'))

    def test_maybe_cdrom_device_true_on_hd_partitions(self):
        """Test maybe_cdrom_device is false on /dev/hd[a-z][0-9]+ paths"""
        self.assertTrue(dsovf.maybe_cdrom_device('/dev/hda1'))
        self.assertTrue(dsovf.maybe_cdrom_device('hdz9'))

    def test_maybe_cdrom_device_true_on_valid_relative_paths(self):
        """Test maybe_cdrom_device normalizes paths"""
        self.assertTrue(dsovf.maybe_cdrom_device('/dev/wark/../sr9'))
        self.assertTrue(dsovf.maybe_cdrom_device('///sr0'))
        self.assertTrue(dsovf.maybe_cdrom_device('/sr0'))
        self.assertTrue(dsovf.maybe_cdrom_device('//dev//hda'))

    def test_maybe_cdrom_device_true_on_xvd_partitions(self):
        """Test maybe_cdrom_device returns true on xvd*"""
        self.assertTrue(dsovf.maybe_cdrom_device('/dev/xvda'))
        self.assertTrue(dsovf.maybe_cdrom_device('/dev/xvda1'))
        self.assertTrue(dsovf.maybe_cdrom_device('xvdza1'))

#
# vi: ts=4 expandtab
