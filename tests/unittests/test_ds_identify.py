# This file is part of cloud-init. See LICENSE file for license information.

import copy
import os
from uuid import uuid4

from cloudinit import safeyaml
from cloudinit import util
from cloudinit.tests.helpers import (
    CiTestCase, dir2dict, json_dumps, populate_dir)

UNAME_MYSYS = ("Linux bart 4.4.0-62-generic #83-Ubuntu "
               "SMP Wed Jan 18 14:10:15 UTC 2017 x86_64 GNU/Linux")
UNAME_PPC64EL = ("Linux diamond 4.4.0-83-generic #106-Ubuntu SMP "
                 "Mon Jun 26 17:53:54 UTC 2017 "
                 "ppc64le ppc64le ppc64le GNU/Linux")

BLKID_EFI_ROOT = """
DEVNAME=/dev/sda1
UUID=8B36-5390
TYPE=vfat
PARTUUID=30d7c715-a6ae-46ee-b050-afc6467fc452

DEVNAME=/dev/sda2
UUID=19ac97d5-6973-4193-9a09-2e6bbfa38262
TYPE=ext4
PARTUUID=30c65c77-e07d-4039-b2fb-88b1fb5fa1fc
"""

POLICY_FOUND_ONLY = "search,found=all,maybe=none,notfound=disabled"
POLICY_FOUND_OR_MAYBE = "search,found=all,maybe=all,notfound=disabled"
DI_DEFAULT_POLICY = "search,found=all,maybe=all,notfound=enabled"
DI_DEFAULT_POLICY_NO_DMI = "search,found=all,maybe=all,notfound=disabled"
DI_EC2_STRICT_ID_DEFAULT = "true"

SHELL_MOCK_TMPL = """\
%(name)s() {
   local out='%(out)s' err='%(err)s' r='%(ret)s' RET='%(RET)s'
   [ "$out" = "_unset" ] || echo "$out"
   [ "$err" = "_unset" ] || echo "$err" 2>&1
   [ "$RET" = "_unset" ] || _RET="$RET"
   return $r
}
"""

RC_FOUND = 0
RC_NOT_FOUND = 1
DS_NONE = 'None'

P_CHASSIS_ASSET_TAG = "sys/class/dmi/id/chassis_asset_tag"
P_PRODUCT_NAME = "sys/class/dmi/id/product_name"
P_PRODUCT_SERIAL = "sys/class/dmi/id/product_serial"
P_PRODUCT_UUID = "sys/class/dmi/id/product_uuid"
P_SEED_DIR = "var/lib/cloud/seed"
P_DSID_CFG = "etc/cloud/ds-identify.cfg"

MOCK_VIRT_IS_KVM = {'name': 'detect_virt', 'RET': 'kvm', 'ret': 0}
MOCK_UNAME_IS_PPC64 = {'name': 'uname', 'out': UNAME_PPC64EL, 'ret': 0}


class TestDsIdentify(CiTestCase):
    dsid_path = os.path.realpath('tools/ds-identify')

    def call(self, rootd=None, mocks=None, args=None, files=None,
             policy_dmi=DI_DEFAULT_POLICY,
             policy_no_dmi=DI_DEFAULT_POLICY_NO_DMI,
             ec2_strict_id=DI_EC2_STRICT_ID_DEFAULT):
        if args is None:
            args = []
        if mocks is None:
            mocks = []

        if files is None:
            files = {}

        if rootd is None:
            rootd = self.tmp_dir()

        unset = '_unset'
        wrap = self.tmp_path(path="_shwrap", dir=rootd)
        populate_dir(rootd, files)

        # DI_DEFAULT_POLICY* are declared always as to not rely
        # on the default in the code.  This is because SRU releases change
        # the value in the code, and thus tests would fail there.
        head = [
            "DI_MAIN=noop",
            "DEBUG_LEVEL=2",
            "DI_LOG=stderr",
            "PATH_ROOT='%s'" % rootd,
            ". " + self.dsid_path,
            'DI_DEFAULT_POLICY="%s"' % policy_dmi,
            'DI_DEFAULT_POLICY_NO_DMI="%s"' % policy_no_dmi,
            'DI_EC2_STRICT_ID_DEFAULT="%s"' % ec2_strict_id,
            ""
        ]

        def write_mock(data):
            ddata = {'out': None, 'err': None, 'ret': 0, 'RET': None}
            ddata.update(data)
            for k in ddata:
                if ddata[k] is None:
                    ddata[k] = unset
            return SHELL_MOCK_TMPL % ddata

        mocklines = []
        defaults = [
            {'name': 'detect_virt', 'RET': 'none', 'ret': 1},
            {'name': 'uname', 'out': UNAME_MYSYS},
            {'name': 'blkid', 'out': BLKID_EFI_ROOT},
        ]

        written = [d['name'] for d in mocks]
        for data in mocks:
            mocklines.append(write_mock(data))
        for d in defaults:
            if d['name'] not in written:
                mocklines.append(write_mock(d))

        endlines = [
            'main %s' % ' '.join(['"%s"' % s for s in args])
        ]

        with open(wrap, "w") as fp:
            fp.write('\n'.join(head + mocklines + endlines) + "\n")

        rc = 0
        try:
            out, err = util.subp(['sh', '-c', '. %s' % wrap], capture=True)
        except util.ProcessExecutionError as e:
            rc = e.exit_code
            out = e.stdout
            err = e.stderr

        cfg = None
        cfg_out = os.path.join(rootd, 'run/cloud-init/cloud.cfg')
        if os.path.exists(cfg_out):
            contents = util.load_file(cfg_out)
            try:
                cfg = safeyaml.load(contents)
            except Exception as e:
                cfg = {"_INVALID_YAML": contents,
                       "_EXCEPTION": str(e)}

        return rc, out, err, cfg, dir2dict(rootd)

    def _call_via_dict(self, data, rootd=None, **kwargs):
        # return output of self.call with a dict input like VALID_CFG[item]
        xwargs = {'rootd': rootd}
        for k in ('mocks', 'args', 'policy_dmi', 'policy_no_dmi', 'files'):
            if k in data:
                xwargs[k] = data[k]
            if k in kwargs:
                xwargs[k] = kwargs[k]

        return self.call(**xwargs)

    def _test_ds_found(self, name):
        data = copy.deepcopy(VALID_CFG[name])
        return self._check_via_dict(
            data, RC_FOUND, dslist=[data.get('ds'), DS_NONE])

    def _check_via_dict(self, data, rc, dslist=None, **kwargs):
        found_rc, out, err, cfg, files = self._call_via_dict(data, **kwargs)
        good = False
        try:
            self.assertEqual(rc, found_rc)
            if dslist is not None:
                self.assertEqual(dslist, cfg['datasource_list'])
            good = True
        finally:
            if not good:
                _print_run_output(rc, out, err, cfg, files)
        return rc, out, err, cfg, files

    def test_wb_print_variables(self):
        """_print_info reports an array of discovered variables to stderr."""
        data = VALID_CFG['Azure-dmi-detection']
        _, _, err, _, _ = self._call_via_dict(data)
        expected_vars = [
            'DMI_PRODUCT_NAME', 'DMI_SYS_VENDOR', 'DMI_PRODUCT_SERIAL',
            'DMI_PRODUCT_UUID', 'PID_1_PRODUCT_NAME', 'DMI_CHASSIS_ASSET_TAG',
            'FS_LABELS', 'KERNEL_CMDLINE', 'VIRT', 'UNAME_KERNEL_NAME',
            'UNAME_KERNEL_RELEASE', 'UNAME_KERNEL_VERSION', 'UNAME_MACHINE',
            'UNAME_NODENAME', 'UNAME_OPERATING_SYSTEM', 'DSNAME', 'DSLIST',
            'MODE', 'ON_FOUND', 'ON_MAYBE', 'ON_NOTFOUND']
        for var in expected_vars:
            self.assertIn('{0}='.format(var), err)

    def test_azure_dmi_detection_from_chassis_asset_tag(self):
        """Azure datasource is detected from DMI chassis-asset-tag"""
        self._test_ds_found('Azure-dmi-detection')

    def test_azure_seed_file_detection(self):
        """Azure datasource is detected due to presence of a seed file.

        The seed file tested  is /var/lib/cloud/seed/azure/ovf-env.xml."""
        self._test_ds_found('Azure-seed-detection')

    def test_aws_ec2_hvm(self):
        """EC2: hvm instances use dmi serial and uuid starting with 'ec2'."""
        self._test_ds_found('Ec2-hvm')

    def test_aws_ec2_xen(self):
        """EC2: sys/hypervisor/uuid starts with ec2."""
        self._test_ds_found('Ec2-xen')

    def test_brightbox_is_ec2(self):
        """EC2: product_serial ends with 'brightbox.com'"""
        self._test_ds_found('Ec2-brightbox')

    def test_gce_by_product_name(self):
        """GCE identifies itself with product_name."""
        self._test_ds_found('GCE')

    def test_gce_by_serial(self):
        """Older gce compute instances must be identified by serial."""
        self._test_ds_found('GCE-serial')

    def test_config_drive(self):
        """ConfigDrive datasource has a disk with LABEL=config-2."""
        self._test_ds_found('ConfigDrive')
        return

    def test_policy_disabled(self):
        """A Builtin policy of 'disabled' should return not found.

        Even though a search would find something, the builtin policy of
        disabled should cause the return of not found."""
        mydata = copy.deepcopy(VALID_CFG['Ec2-hvm'])
        self._check_via_dict(mydata, rc=RC_NOT_FOUND, policy_dmi="disabled")

    def test_policy_config_disable_overrides_builtin(self):
        """explicit policy: disabled in config file should cause not found."""
        mydata = copy.deepcopy(VALID_CFG['Ec2-hvm'])
        mydata['files'][P_DSID_CFG] = '\n'.join(['policy: disabled', ''])
        self._check_via_dict(mydata, rc=RC_NOT_FOUND)

    def test_single_entry_defines_datasource(self):
        """If config has a single entry in datasource_list, that is used.

        Test the valid Ec2-hvm, but provide a config file that specifies
        a single entry in datasource_list.  The configured value should
        be used."""
        mydata = copy.deepcopy(VALID_CFG['Ec2-hvm'])
        cfgpath = 'etc/cloud/cloud.cfg.d/myds.cfg'
        mydata['files'][cfgpath] = 'datasource_list: ["NoCloud"]\n'
        self._check_via_dict(mydata, rc=RC_FOUND, dslist=['NoCloud', DS_NONE])

    def test_configured_list_with_none(self):
        """When datasource_list already contains None, None is not added.

        The explicitly configured datasource_list has 'None' in it.  That
        should not have None automatically added."""
        mydata = copy.deepcopy(VALID_CFG['GCE'])
        cfgpath = 'etc/cloud/cloud.cfg.d/myds.cfg'
        mydata['files'][cfgpath] = 'datasource_list: ["Ec2", "None"]\n'
        self._check_via_dict(mydata, rc=RC_FOUND, dslist=['Ec2', DS_NONE])

    def test_aliyun_identified(self):
        """Test that Aliyun cloud is identified by product id."""
        self._test_ds_found('AliYun')

    def test_aliyun_over_ec2(self):
        """Even if all other factors identified Ec2, AliYun should be used."""
        mydata = copy.deepcopy(VALID_CFG['Ec2-xen'])
        self._test_ds_found('AliYun')
        prod_name = VALID_CFG['AliYun']['files'][P_PRODUCT_NAME]
        mydata['files'][P_PRODUCT_NAME] = prod_name
        policy = "search,found=first,maybe=none,notfound=disabled"
        self._check_via_dict(mydata, rc=RC_FOUND, dslist=['AliYun', DS_NONE],
                             policy_dmi=policy)

    def test_default_openstack_intel_is_found(self):
        """On Intel, openstack must be identified."""
        self._test_ds_found('OpenStack')

    def test_openstack_on_non_intel_is_maybe(self):
        """On non-Intel, openstack without dmi info is maybe.

        nova does not identify itself on platforms other than intel.
           https://bugs.launchpad.net/cloud-init/+bugs?field.tag=dsid-nova"""

        data = VALID_CFG['OpenStack'].copy()
        del data['files'][P_PRODUCT_NAME]
        data.update({'policy_dmi': POLICY_FOUND_OR_MAYBE,
                     'policy_no_dmi': POLICY_FOUND_OR_MAYBE})

        # this should show not found as default uname in tests is intel.
        # and intel openstack requires positive identification.
        self._check_via_dict(data, RC_NOT_FOUND, dslist=None)

        # updating the uname to ppc64 though should get a maybe.
        data.update({'mocks': [MOCK_VIRT_IS_KVM, MOCK_UNAME_IS_PPC64]})
        (_, _, err, _, _) = self._check_via_dict(
            data, RC_FOUND, dslist=['OpenStack', 'None'])
        self.assertIn("check for 'OpenStack' returned maybe", err)


def blkid_out(disks=None):
    """Convert a list of disk dictionaries into blkid content."""
    if disks is None:
        disks = []
    lines = []
    for disk in disks:
        if not disk["DEVNAME"].startswith("/dev/"):
            disk["DEVNAME"] = "/dev/" + disk["DEVNAME"]
        for key in disk:
            lines.append("%s=%s" % (key, disk[key]))
        lines.append("")
    return '\n'.join(lines)


def _print_run_output(rc, out, err, cfg, files):
    """A helper to print return of TestDsIdentify.

       _print_run_output(self.call())"""
    print('\n'.join([
        '-- rc = %s --' % rc,
        '-- out --', str(out),
        '-- err --', str(err),
        '-- cfg --', json_dumps(cfg)]))
    print('-- files --')
    for k, v in files.items():
        if "/_shwrap" in k:
            continue
        print(' === %s ===' % k)
        for line in v.splitlines():
            print(" " + line)


VALID_CFG = {
    'AliYun': {
        'ds': 'AliYun',
        'files': {P_PRODUCT_NAME: 'Alibaba Cloud ECS\n'},
    },
    'Azure-dmi-detection': {
        'ds': 'Azure',
        'files': {
            P_CHASSIS_ASSET_TAG: '7783-7084-3265-9085-8269-3286-77\n',
        }
    },
    'Azure-seed-detection': {
        'ds': 'Azure',
        'files': {
            P_CHASSIS_ASSET_TAG: 'No-match\n',
            os.path.join(P_SEED_DIR, 'azure', 'ovf-env.xml'): 'present\n',
        }
    },
    'Ec2-hvm': {
        'ds': 'Ec2',
        'mocks': [{'name': 'detect_virt', 'RET': 'kvm', 'ret': 0}],
        'files': {
            P_PRODUCT_SERIAL: 'ec23aef5-54be-4843-8d24-8c819f88453e\n',
            P_PRODUCT_UUID: 'EC23AEF5-54BE-4843-8D24-8C819F88453E\n',
        }
    },
    'Ec2-xen': {
        'ds': 'Ec2',
        'mocks': [{'name': 'detect_virt', 'RET': 'xen', 'ret': 0}],
        'files': {
            'sys/hypervisor/uuid': 'ec2c6e2f-5fac-4fc7-9c82-74127ec14bbb\n'
        },
    },
    'Ec2-brightbox': {
        'ds': 'Ec2',
        'files': {P_PRODUCT_SERIAL: 'facc6e2f.brightbox.com\n'},
    },
    'GCE': {
        'ds': 'GCE',
        'files': {P_PRODUCT_NAME: 'Google Compute Engine\n'},
        'mocks': [MOCK_VIRT_IS_KVM],
    },
    'GCE-serial': {
        'ds': 'GCE',
        'files': {P_PRODUCT_SERIAL: 'GoogleCloud-8f2e88f\n'},
        'mocks': [MOCK_VIRT_IS_KVM],
    },
    'OpenStack': {
        'ds': 'OpenStack',
        'files': {P_PRODUCT_NAME: 'OpenStack Nova\n'},
        'mocks': [MOCK_VIRT_IS_KVM],
        'policy_dmi': POLICY_FOUND_ONLY,
        'policy_no_dmi': POLICY_FOUND_ONLY,
    },
    'ConfigDrive': {
        'ds': 'ConfigDrive',
        'mocks': [
            {'name': 'blkid', 'ret': 0,
             'out': blkid_out(
                 [{'DEVNAME': 'vda1', 'TYPE': 'vfat', 'PARTUUID': uuid4()},
                  {'DEVNAME': 'vda2', 'TYPE': 'ext4',
                   'LABEL': 'cloudimg-rootfs', 'PARTUUID': uuid4()},
                  {'DEVNAME': 'vdb', 'TYPE': 'vfat', 'LABEL': 'config-2'}])
             },
        ],
    },
}

# vi: ts=4 expandtab
