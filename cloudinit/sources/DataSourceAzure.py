# Copyright (C) 2013 Canonical Ltd.
#
# Author: Scott Moser <scott.moser@canonical.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import base64
import contextlib
import crypt
from functools import partial
import os
import os.path
import re
import time
from xml.dom import minidom
import xml.etree.ElementTree as ET

from cloudinit import log as logging
from cloudinit import net
from cloudinit import sources
from cloudinit.sources.helpers.azure import get_metadata_from_fabric
from cloudinit import util

LOG = logging.getLogger(__name__)

DS_NAME = 'Azure'
DEFAULT_METADATA = {"instance-id": "iid-AZURE-NODE"}
AGENT_START = ['service', 'walinuxagent', 'start']
AGENT_START_BUILTIN = "__builtin__"
BOUNCE_COMMAND = [
    'sh', '-xc',
    "i=$interface; x=0; ifdown $i || x=$?; ifup $i || x=$?; exit $x"
]
# azure systems will always have a resource disk, and 66-azure-ephemeral.rules
# ensures that it gets linked to this path.
RESOURCE_DISK_PATH = '/dev/disk/cloud/azure_resource'
DEFAULT_PRIMARY_NIC = 'eth0'
LEASE_FILE = '/var/lib/dhcp/dhclient.eth0.leases'
DEFAULT_FS = 'ext4'
# DMI chassis-asset-tag is set static for all azure instances
AZURE_CHASSIS_ASSET_TAG = '7783-7084-3265-9085-8269-3286-77'


def find_storvscid_from_sysctl_pnpinfo(sysctl_out, deviceid):
    # extract the 'X' from dev.storvsc.X. if deviceid matches
    """
    dev.storvsc.1.%pnpinfo:
        classid=32412632-86cb-44a2-9b5c-50d1417354f5
        deviceid=00000000-0001-8899-0000-000000000000
    """
    for line in sysctl_out.splitlines():
        if re.search(r"pnpinfo", line):
            fields = line.split()
            if len(fields) >= 3:
                columns = fields[2].split('=')
                if (len(columns) >= 2 and
                        columns[0] == "deviceid" and
                        columns[1].startswith(deviceid)):
                    comps = fields[0].split('.')
                    return comps[2]
    return None


def find_busdev_from_disk(camcontrol_out, disk_drv):
    # find the scbusX from 'camcontrol devlist -b' output
    # if disk_drv matches the specified disk driver, i.e. blkvsc1
    """
    scbus0 on ata0 bus 0
    scbus1 on ata1 bus 0
    scbus2 on blkvsc0 bus 0
    scbus3 on blkvsc1 bus 0
    scbus4 on storvsc2 bus 0
    scbus5 on storvsc3 bus 0
    scbus-1 on xpt0 bus 0
    """
    for line in camcontrol_out.splitlines():
        if re.search(disk_drv, line):
            items = line.split()
            return items[0]
    return None


def find_dev_from_busdev(camcontrol_out, busdev):
    # find the daX from 'camcontrol devlist' output
    # if busdev matches the specified value, i.e. 'scbus2'
    """
    <Msft Virtual CD/ROM 1.0>          at scbus1 target 0 lun 0 (cd0,pass0)
    <Msft Virtual Disk 1.0>            at scbus2 target 0 lun 0 (da0,pass1)
    <Msft Virtual Disk 1.0>            at scbus3 target 1 lun 0 (da1,pass2)
    """
    for line in camcontrol_out.splitlines():
        if re.search(busdev, line):
            items = line.split('(')
            if len(items) == 2:
                dev_pass = items[1].split(',')
                return dev_pass[0]
    return None


def get_dev_storvsc_sysctl():
    try:
        sysctl_out, err = util.subp(['sysctl', 'dev.storvsc'])
    except util.ProcessExecutionError:
        LOG.debug("Fail to execute sysctl dev.storvsc")
        sysctl_out = ""
    return sysctl_out


def get_camcontrol_dev_bus():
    try:
        camcontrol_b_out, err = util.subp(['camcontrol', 'devlist', '-b'])
    except util.ProcessExecutionError:
        LOG.debug("Fail to execute camcontrol devlist -b")
        return None
    return camcontrol_b_out


def get_camcontrol_dev():
    try:
        camcontrol_out, err = util.subp(['camcontrol', 'devlist'])
    except util.ProcessExecutionError:
        LOG.debug("Fail to execute camcontrol devlist")
        return None
    return camcontrol_out


def get_resource_disk_on_freebsd(port_id):
    g0 = "00000000"
    if port_id > 1:
        g0 = "00000001"
        port_id = port_id - 2
    g1 = "000" + str(port_id)
    g0g1 = "{0}-{1}".format(g0, g1)
    """
    search 'X' from
       'dev.storvsc.X.%pnpinfo:
           classid=32412632-86cb-44a2-9b5c-50d1417354f5
           deviceid=00000000-0001-8899-0000-000000000000'
    """
    sysctl_out = get_dev_storvsc_sysctl()

    storvscid = find_storvscid_from_sysctl_pnpinfo(sysctl_out, g0g1)
    if not storvscid:
        LOG.debug("Fail to find storvsc id from sysctl")
        return None

    camcontrol_b_out = get_camcontrol_dev_bus()
    camcontrol_out = get_camcontrol_dev()
    # try to find /dev/XX from 'blkvsc' device
    blkvsc = "blkvsc{0}".format(storvscid)
    scbusx = find_busdev_from_disk(camcontrol_b_out, blkvsc)
    if scbusx:
        devname = find_dev_from_busdev(camcontrol_out, scbusx)
        if devname is None:
            LOG.debug("Fail to find /dev/daX")
            return None
        return devname
    # try to find /dev/XX from 'storvsc' device
    storvsc = "storvsc{0}".format(storvscid)
    scbusx = find_busdev_from_disk(camcontrol_b_out, storvsc)
    if scbusx:
        devname = find_dev_from_busdev(camcontrol_out, scbusx)
        if devname is None:
            LOG.debug("Fail to find /dev/daX")
            return None
        return devname
    return None


# update the FreeBSD specific information
if util.is_FreeBSD():
    DEFAULT_PRIMARY_NIC = 'hn0'
    LEASE_FILE = '/var/db/dhclient.leases.hn0'
    DEFAULT_FS = 'freebsd-ufs'
    res_disk = get_resource_disk_on_freebsd(1)
    if res_disk is not None:
        LOG.debug("resource disk is not None")
        RESOURCE_DISK_PATH = "/dev/" + res_disk
    else:
        LOG.debug("resource disk is None")
    BOUNCE_COMMAND = [
        'sh', '-xc',
        ("i=$interface; x=0; ifconfig down $i || x=$?; "
         "ifconfig up $i || x=$?; exit $x")
    ]

BUILTIN_DS_CONFIG = {
    'agent_command': AGENT_START_BUILTIN,
    'data_dir': "/var/lib/waagent",
    'set_hostname': True,
    'hostname_bounce': {
        'interface': DEFAULT_PRIMARY_NIC,
        'policy': True,
        'command': BOUNCE_COMMAND,
        'hostname_command': 'hostname',
    },
    'disk_aliases': {'ephemeral0': RESOURCE_DISK_PATH},
    'dhclient_lease_file': LEASE_FILE,
}

BUILTIN_CLOUD_CONFIG = {
    'disk_setup': {
        'ephemeral0': {'table_type': 'gpt',
                       'layout': [100],
                       'overwrite': True},
    },
    'fs_setup': [{'filesystem': DEFAULT_FS,
                  'device': 'ephemeral0.1'}],
}

DS_CFG_PATH = ['datasource', DS_NAME]
DEF_EPHEMERAL_LABEL = 'Temporary Storage'

# The redacted password fails to meet password complexity requirements
# so we can safely use this to mask/redact the password in the ovf-env.xml
DEF_PASSWD_REDACTION = 'REDACTED'


def get_hostname(hostname_command='hostname'):
    return util.subp(hostname_command, capture=True)[0].strip()


def set_hostname(hostname, hostname_command='hostname'):
    util.subp([hostname_command, hostname])


@contextlib.contextmanager
def temporary_hostname(temp_hostname, cfg, hostname_command='hostname'):
    """
    Set a temporary hostname, restoring the previous hostname on exit.

    Will have the value of the previous hostname when used as a context
    manager, or None if the hostname was not changed.
    """
    policy = cfg['hostname_bounce']['policy']
    previous_hostname = get_hostname(hostname_command)
    if (not util.is_true(cfg.get('set_hostname')) or
       util.is_false(policy) or
       (previous_hostname == temp_hostname and policy != 'force')):
        yield None
        return
    set_hostname(temp_hostname, hostname_command)
    try:
        yield previous_hostname
    finally:
        set_hostname(previous_hostname, hostname_command)


class DataSourceAzure(sources.DataSource):
    _negotiated = False

    def __init__(self, sys_cfg, distro, paths):
        sources.DataSource.__init__(self, sys_cfg, distro, paths)
        self.seed_dir = os.path.join(paths.seed_dir, 'azure')
        self.cfg = {}
        self.seed = None
        self.ds_cfg = util.mergemanydict([
            util.get_cfg_by_path(sys_cfg, DS_CFG_PATH, {}),
            BUILTIN_DS_CONFIG])
        self.dhclient_lease_file = self.ds_cfg.get('dhclient_lease_file')
        self._network_config = None

    def __str__(self):
        root = sources.DataSource.__str__(self)
        return "%s [seed=%s]" % (root, self.seed)

    def bounce_network_with_azure_hostname(self):
        # When using cloud-init to provision, we have to set the hostname from
        # the metadata and "bounce" the network to force DDNS to update via
        # dhclient
        azure_hostname = self.metadata.get('local-hostname')
        LOG.debug("Hostname in metadata is %s", azure_hostname)
        hostname_command = self.ds_cfg['hostname_bounce']['hostname_command']

        with temporary_hostname(azure_hostname, self.ds_cfg,
                                hostname_command=hostname_command) \
                as previous_hostname:
            if (previous_hostname is not None and
                    util.is_true(self.ds_cfg.get('set_hostname'))):
                cfg = self.ds_cfg['hostname_bounce']

                # "Bouncing" the network
                try:
                    perform_hostname_bounce(hostname=azure_hostname,
                                            cfg=cfg,
                                            prev_hostname=previous_hostname)
                except Exception as e:
                    LOG.warning("Failed publishing hostname: %s", e)
                    util.logexc(LOG, "handling set_hostname failed")

    def get_metadata_from_agent(self):
        temp_hostname = self.metadata.get('local-hostname')
        agent_cmd = self.ds_cfg['agent_command']
        LOG.debug("Getting metadata via agent.  hostname=%s cmd=%s",
                  temp_hostname, agent_cmd)

        self.bounce_network_with_azure_hostname()

        try:
            invoke_agent(agent_cmd)
        except util.ProcessExecutionError:
            # claim the datasource even if the command failed
            util.logexc(LOG, "agent command '%s' failed.",
                        self.ds_cfg['agent_command'])

        ddir = self.ds_cfg['data_dir']

        fp_files = []
        key_value = None
        for pk in self.cfg.get('_pubkeys', []):
            if pk.get('value', None):
                key_value = pk['value']
                LOG.debug("ssh authentication: using value from fabric")
            else:
                bname = str(pk['fingerprint'] + ".crt")
                fp_files += [os.path.join(ddir, bname)]
                LOG.debug("ssh authentication: "
                          "using fingerprint from fabirc")

        # wait very long for public SSH keys to arrive
        # https://bugs.launchpad.net/cloud-init/+bug/1717611
        missing = util.log_time(logfunc=LOG.debug,
                                msg="waiting for SSH public key files",
                                func=wait_for_files,
                                args=(fp_files, 900))

        if len(missing):
            LOG.warning("Did not find files, but going on: %s", missing)

        metadata = {}
        metadata['public-keys'] = key_value or pubkeys_from_crt_files(fp_files)
        return metadata

    def get_data(self):
        # azure removes/ejects the cdrom containing the ovf-env.xml
        # file on reboot.  So, in order to successfully reboot we
        # need to look in the datadir and consider that valid
        asset_tag = util.read_dmi_data('chassis-asset-tag')
        if asset_tag != AZURE_CHASSIS_ASSET_TAG:
            LOG.debug("Non-Azure DMI asset tag '%s' discovered.", asset_tag)
            return False

        ddir = self.ds_cfg['data_dir']

        candidates = [self.seed_dir]
        candidates.extend(list_possible_azure_ds_devs())
        if ddir:
            candidates.append(ddir)

        found = None

        for cdev in candidates:
            try:
                if cdev.startswith("/dev/"):
                    if util.is_FreeBSD():
                        ret = util.mount_cb(cdev, load_azure_ds_dir,
                                            mtype="udf", sync=False)
                    else:
                        ret = util.mount_cb(cdev, load_azure_ds_dir)
                else:
                    ret = load_azure_ds_dir(cdev)

            except NonAzureDataSource:
                continue
            except BrokenAzureDataSource as exc:
                raise exc
            except util.MountFailedError:
                LOG.warning("%s was not mountable", cdev)
                continue

            (md, self.userdata_raw, cfg, files) = ret
            self.seed = cdev
            self.metadata = util.mergemanydict([md, DEFAULT_METADATA])
            self.cfg = util.mergemanydict([cfg, BUILTIN_CLOUD_CONFIG])
            found = cdev

            LOG.debug("found datasource in %s", cdev)
            break

        if not found:
            return False

        if found == ddir:
            LOG.debug("using files cached in %s", ddir)

        # azure / hyper-v provides random data here
        # TODO. find the seed on FreeBSD platform
        # now update ds_cfg to reflect contents pass in config
        if not util.is_FreeBSD():
            seed = util.load_file("/sys/firmware/acpi/tables/OEM0",
                                  quiet=True, decode=False)
            if seed:
                self.metadata['random_seed'] = seed

        user_ds_cfg = util.get_cfg_by_path(self.cfg, DS_CFG_PATH, {})
        self.ds_cfg = util.mergemanydict([user_ds_cfg, self.ds_cfg])

        # walinux agent writes files world readable, but expects
        # the directory to be protected.
        write_files(ddir, files, dirmode=0o700)

        self.metadata['instance-id'] = util.read_dmi_data('system-uuid')

        return True

    def device_name_to_device(self, name):
        return self.ds_cfg['disk_aliases'].get(name)

    def get_config_obj(self):
        return self.cfg

    def check_instance_id(self, sys_cfg):
        # quickly (local check only) if self.instance_id is still valid
        return sources.instance_id_matches_system_uuid(self.get_instance_id())

    def setup(self, is_new_instance):
        if self._negotiated is False:
            LOG.debug("negotiating for %s (new_instance=%s)",
                      self.get_instance_id(), is_new_instance)
            fabric_data = self._negotiate()
            LOG.debug("negotiating returned %s", fabric_data)
            if fabric_data:
                self.metadata.update(fabric_data)
            self._negotiated = True
        else:
            LOG.debug("negotiating already done for %s",
                      self.get_instance_id())

    def _negotiate(self):
        """Negotiate with fabric and return data from it.

           On success, returns a dictionary including 'public_keys'.
           On failure, returns False.
        """

        if self.ds_cfg['agent_command'] == AGENT_START_BUILTIN:
            self.bounce_network_with_azure_hostname()

            metadata_func = partial(get_metadata_from_fabric,
                                    fallback_lease_file=self.
                                    dhclient_lease_file)
        else:
            metadata_func = self.get_metadata_from_agent

        LOG.debug("negotiating with fabric via agent command %s",
                  self.ds_cfg['agent_command'])
        try:
            fabric_data = metadata_func()
        except Exception as exc:
            LOG.warning(
                "Error communicating with Azure fabric; You may experience."
                "connectivity issues.", exc_info=True)
            return False

        return fabric_data

    def activate(self, cfg, is_new_instance):
        address_ephemeral_resize(is_new_instance=is_new_instance)
        return

    @property
    def network_config(self):
        """Generate a network config like net.generate_fallback_network() with
           the following execptions.

           1. Probe the drivers of the net-devices present and inject them in
              the network configuration under params: driver: <driver> value
           2. If the driver value is 'mlx4_core', the control mode should be
              set to manual.  The device will be later used to build a bond,
              for now we want to ensure the device gets named but does not
              break any network configuration
        """
        blacklist = ['mlx4_core']
        if not self._network_config:
            LOG.debug('Azure: generating fallback configuration')
            # generate a network config, blacklist picking any mlx4_core devs
            netconfig = net.generate_fallback_config(
                blacklist_drivers=blacklist, config_driver=True)

            # if we have any blacklisted devices, update the network_config to
            # include the device, mac, and driver values, but with no ip
            # config; this ensures udev rules are generated but won't affect
            # ip configuration
            bl_found = 0
            for bl_dev in [dev for dev in net.get_devicelist()
                           if net.device_driver(dev) in blacklist]:
                bl_found += 1
                cfg = {
                    'type': 'physical',
                    'name': 'vf%d' % bl_found,
                    'mac_address': net.get_interface_mac(bl_dev),
                    'params': {
                        'driver': net.device_driver(bl_dev),
                        'device_id': net.device_devid(bl_dev),
                    },
                }
                netconfig['config'].append(cfg)

            self._network_config = netconfig

        return self._network_config


def _partitions_on_device(devpath, maxnum=16):
    # return a list of tuples (ptnum, path) for each part on devpath
    for suff in ("-part", "p", ""):
        found = []
        for pnum in range(1, maxnum):
            ppath = devpath + suff + str(pnum)
            if os.path.exists(ppath):
                found.append((pnum, os.path.realpath(ppath)))
        if found:
            return found
    return []


def _has_ntfs_filesystem(devpath):
    ntfs_devices = util.find_devs_with("TYPE=ntfs", no_cache=True)
    LOG.debug('ntfs_devices found = %s', ntfs_devices)
    return os.path.realpath(devpath) in ntfs_devices


def can_dev_be_reformatted(devpath):
    """Determine if block device devpath is newly formatted ephemeral.

    A newly formatted disk will:
      a.) have a partition table (dos or gpt)
      b.) have 1 partition that is ntfs formatted, or
          have 2 partitions with the second partition ntfs formatted.
          (larger instances with >2TB ephemeral disk have gpt, and will
           have a microsoft reserved partition as part 1.  LP: #1686514)
      c.) the ntfs partition will have no files other than possibly
          'dataloss_warning_readme.txt'"""
    if not os.path.exists(devpath):
        return False, 'device %s does not exist' % devpath

    LOG.debug('Resolving realpath of %s -> %s', devpath,
              os.path.realpath(devpath))

    # devpath of /dev/sd[a-z] or /dev/disk/cloud/azure_resource
    # where partitions are "<devpath>1" or "<devpath>-part1" or "<devpath>p1"
    partitions = _partitions_on_device(devpath)
    if len(partitions) == 0:
        return False, 'device %s was not partitioned' % devpath
    elif len(partitions) > 2:
        msg = ('device %s had 3 or more partitions: %s' %
               (devpath, ' '.join([p[1] for p in partitions])))
        return False, msg
    elif len(partitions) == 2:
        cand_part, cand_path = partitions[1]
    else:
        cand_part, cand_path = partitions[0]

    if not _has_ntfs_filesystem(cand_path):
        msg = ('partition %s (%s) on device %s was not ntfs formatted' %
               (cand_part, cand_path, devpath))
        return False, msg

    def count_files(mp):
        ignored = set(['dataloss_warning_readme.txt'])
        return len([f for f in os.listdir(mp) if f.lower() not in ignored])

    bmsg = ('partition %s (%s) on device %s was ntfs formatted' %
            (cand_part, cand_path, devpath))
    try:
        file_count = util.mount_cb(cand_path, count_files)
    except util.MountFailedError as e:
        return False, bmsg + ' but mount of %s failed: %s' % (cand_part, e)

    if file_count != 0:
        return False, bmsg + ' but had %d files on it.' % file_count

    return True, bmsg + ' and had no important files. Safe for reformatting.'


def address_ephemeral_resize(devpath=RESOURCE_DISK_PATH, maxwait=120,
                             is_new_instance=False):
    # wait for ephemeral disk to come up
    naplen = .2
    missing = wait_for_files([devpath], maxwait=maxwait, naplen=naplen,
                             log_pre="Azure ephemeral disk: ")

    if missing:
        LOG.warning("ephemeral device '%s' did not appear after %d seconds.",
                    devpath, maxwait)
        return

    result = False
    msg = None
    if is_new_instance:
        result, msg = (True, "First instance boot.")
    else:
        result, msg = can_dev_be_reformatted(devpath)

    LOG.debug("reformattable=%s: %s", result, msg)
    if not result:
        return

    for mod in ['disk_setup', 'mounts']:
        sempath = '/var/lib/cloud/instance/sem/config_' + mod
        bmsg = 'Marker "%s" for module "%s"' % (sempath, mod)
        if os.path.exists(sempath):
            try:
                os.unlink(sempath)
                LOG.debug(bmsg + " removed.")
            except Exception as e:
                # python3 throws FileNotFoundError, python2 throws OSError
                LOG.warning(bmsg + ": remove failed! (%s)", e)
        else:
            LOG.debug(bmsg + " did not exist.")
    return


def perform_hostname_bounce(hostname, cfg, prev_hostname):
    # set the hostname to 'hostname' if it is not already set to that.
    # then, if policy is not off, bounce the interface using command
    command = cfg['command']
    interface = cfg['interface']
    policy = cfg['policy']

    msg = ("hostname=%s policy=%s interface=%s" %
           (hostname, policy, interface))
    env = os.environ.copy()
    env['interface'] = interface
    env['hostname'] = hostname
    env['old_hostname'] = prev_hostname

    if command == "builtin":
        command = BOUNCE_COMMAND

    LOG.debug("pubhname: publishing hostname [%s]", msg)
    shell = not isinstance(command, (list, tuple))
    # capture=False, see comments in bug 1202758 and bug 1206164.
    util.log_time(logfunc=LOG.debug, msg="publishing hostname",
                  get_uptime=True, func=util.subp,
                  kwargs={'args': command, 'shell': shell, 'capture': False,
                          'env': env})


def crtfile_to_pubkey(fname, data=None):
    pipeline = ('openssl x509 -noout -pubkey < "$0" |'
                'ssh-keygen -i -m PKCS8 -f /dev/stdin')
    (out, _err) = util.subp(['sh', '-c', pipeline, fname],
                            capture=True, data=data)
    return out.rstrip()


def pubkeys_from_crt_files(flist):
    pubkeys = []
    errors = []
    for fname in flist:
        try:
            pubkeys.append(crtfile_to_pubkey(fname))
        except util.ProcessExecutionError:
            errors.append(fname)

    if errors:
        LOG.warning("failed to convert the crt files to pubkey: %s", errors)

    return pubkeys


def wait_for_files(flist, maxwait, naplen=.5, log_pre=""):
    need = set(flist)
    waited = 0
    while True:
        need -= set([f for f in need if os.path.exists(f)])
        if len(need) == 0:
            LOG.debug("%sAll files appeared after %s seconds: %s",
                      log_pre, waited, flist)
            return []
        if waited == 0:
            LOG.info("%sWaiting up to %s seconds for the following files: %s",
                     log_pre, maxwait, flist)
        if waited + naplen > maxwait:
            break
        time.sleep(naplen)
        waited += naplen

    LOG.warning("%sStill missing files after %s seconds: %s",
                log_pre, maxwait, need)
    return need


def write_files(datadir, files, dirmode=None):

    def _redact_password(cnt, fname):
        """Azure provides the UserPassword in plain text. So we redact it"""
        try:
            root = ET.fromstring(cnt)
            for elem in root.iter():
                if ('UserPassword' in elem.tag and
                   elem.text != DEF_PASSWD_REDACTION):
                    elem.text = DEF_PASSWD_REDACTION
            return ET.tostring(root)
        except Exception:
            LOG.critical("failed to redact userpassword in %s", fname)
            return cnt

    if not datadir:
        return
    if not files:
        files = {}
    util.ensure_dir(datadir, dirmode)
    for (name, content) in files.items():
        fname = os.path.join(datadir, name)
        if 'ovf-env.xml' in name:
            content = _redact_password(content, fname)
        util.write_file(filename=fname, content=content, mode=0o600)


def invoke_agent(cmd):
    # this is a function itself to simplify patching it for test
    if cmd:
        LOG.debug("invoking agent: %s", cmd)
        util.subp(cmd, shell=(not isinstance(cmd, list)))
    else:
        LOG.debug("not invoking agent")


def find_child(node, filter_func):
    ret = []
    if not node.hasChildNodes():
        return ret
    for child in node.childNodes:
        if filter_func(child):
            ret.append(child)
    return ret


def load_azure_ovf_pubkeys(sshnode):
    # This parses a 'SSH' node formatted like below, and returns
    # an array of dicts.
    #  [{'fp': '6BE7A7C3C8A8F4B123CCA5D0C2F1BE4CA7B63ED7',
    #    'path': 'where/to/go'}]
    #
    # <SSH><PublicKeys>
    #   <PublicKey><Fingerprint>ABC</FingerPrint><Path>/ABC</Path>
    #   ...
    # </PublicKeys></SSH>
    results = find_child(sshnode, lambda n: n.localName == "PublicKeys")
    if len(results) == 0:
        return []
    if len(results) > 1:
        raise BrokenAzureDataSource("Multiple 'PublicKeys'(%s) in SSH node" %
                                    len(results))

    pubkeys_node = results[0]
    pubkeys = find_child(pubkeys_node, lambda n: n.localName == "PublicKey")

    if len(pubkeys) == 0:
        return []

    found = []
    text_node = minidom.Document.TEXT_NODE

    for pk_node in pubkeys:
        if not pk_node.hasChildNodes():
            continue

        cur = {'fingerprint': "", 'path': "", 'value': ""}
        for child in pk_node.childNodes:
            if child.nodeType == text_node or not child.localName:
                continue

            name = child.localName.lower()

            if name not in cur.keys():
                continue

            if (len(child.childNodes) != 1 or
                    child.childNodes[0].nodeType != text_node):
                continue

            cur[name] = child.childNodes[0].wholeText.strip()
        found.append(cur)

    return found


def read_azure_ovf(contents):
    try:
        dom = minidom.parseString(contents)
    except Exception as e:
        raise BrokenAzureDataSource("Invalid ovf-env.xml: %s" % e)

    results = find_child(dom.documentElement,
                         lambda n: n.localName == "ProvisioningSection")

    if len(results) == 0:
        raise NonAzureDataSource("No ProvisioningSection")
    if len(results) > 1:
        raise BrokenAzureDataSource("found '%d' ProvisioningSection items" %
                                    len(results))
    provSection = results[0]

    lpcs_nodes = find_child(provSection,
                            lambda n:
                            n.localName == "LinuxProvisioningConfigurationSet")

    if len(results) == 0:
        raise NonAzureDataSource("No LinuxProvisioningConfigurationSet")
    if len(results) > 1:
        raise BrokenAzureDataSource("found '%d' %ss" %
                                    ("LinuxProvisioningConfigurationSet",
                                     len(results)))
    lpcs = lpcs_nodes[0]

    if not lpcs.hasChildNodes():
        raise BrokenAzureDataSource("no child nodes of configuration set")

    md_props = 'seedfrom'
    md = {'azure_data': {}}
    cfg = {}
    ud = ""
    password = None
    username = None

    for child in lpcs.childNodes:
        if child.nodeType == dom.TEXT_NODE or not child.localName:
            continue

        name = child.localName.lower()

        simple = False
        value = ""
        if (len(child.childNodes) == 1 and
                child.childNodes[0].nodeType == dom.TEXT_NODE):
            simple = True
            value = child.childNodes[0].wholeText

        attrs = dict([(k, v) for k, v in child.attributes.items()])

        # we accept either UserData or CustomData.  If both are present
        # then behavior is undefined.
        if name == "userdata" or name == "customdata":
            if attrs.get('encoding') in (None, "base64"):
                ud = base64.b64decode(''.join(value.split()))
            else:
                ud = value
        elif name == "username":
            username = value
        elif name == "userpassword":
            password = value
        elif name == "hostname":
            md['local-hostname'] = value
        elif name == "dscfg":
            if attrs.get('encoding') in (None, "base64"):
                dscfg = base64.b64decode(''.join(value.split()))
            else:
                dscfg = value
            cfg['datasource'] = {DS_NAME: util.load_yaml(dscfg, default={})}
        elif name == "ssh":
            cfg['_pubkeys'] = load_azure_ovf_pubkeys(child)
        elif name == "disablesshpasswordauthentication":
            cfg['ssh_pwauth'] = util.is_false(value)
        elif simple:
            if name in md_props:
                md[name] = value
            else:
                md['azure_data'][name] = value

    defuser = {}
    if username:
        defuser['name'] = username
    if password and DEF_PASSWD_REDACTION != password:
        defuser['passwd'] = encrypt_pass(password)
        defuser['lock_passwd'] = False

    if defuser:
        cfg['system_info'] = {'default_user': defuser}

    if 'ssh_pwauth' not in cfg and password:
        cfg['ssh_pwauth'] = True

    return (md, ud, cfg)


def encrypt_pass(password, salt_id="$6$"):
    return crypt.crypt(password, salt_id + util.rand_str(strlen=16))


def _check_freebsd_cdrom(cdrom_dev):
    """Return boolean indicating path to cdrom device has content."""
    try:
        with open(cdrom_dev) as fp:
            fp.read(1024)
            return True
    except IOError:
        LOG.debug("cdrom (%s) is not configured", cdrom_dev)
    return False


def list_possible_azure_ds_devs():
    devlist = []
    if util.is_FreeBSD():
        cdrom_dev = "/dev/cd0"
        if _check_freebsd_cdrom(cdrom_dev):
            return [cdrom_dev]
    else:
        for fstype in ("iso9660", "udf"):
            devlist.extend(util.find_devs_with("TYPE=%s" % fstype))

    devlist.sort(reverse=True)
    return devlist


def load_azure_ds_dir(source_dir):
    ovf_file = os.path.join(source_dir, "ovf-env.xml")

    if not os.path.isfile(ovf_file):
        raise NonAzureDataSource("No ovf-env file found")

    with open(ovf_file, "rb") as fp:
        contents = fp.read()

    md, ud, cfg = read_azure_ovf(contents)
    return (md, ud, cfg, {'ovf-env.xml': contents})


class BrokenAzureDataSource(Exception):
    pass


class NonAzureDataSource(Exception):
    pass


# Legacy: Must be present in case we load an old pkl object
DataSourceAzureNet = DataSourceAzure

# Used to match classes to dependencies
datasources = [
    (DataSourceAzure, (sources.DEP_FILESYSTEM, )),
]


# Return a list of data sources that match this set of dependencies
def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)

# vi: ts=4 expandtab
