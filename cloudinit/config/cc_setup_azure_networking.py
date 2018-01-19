#    Copyright (C) Microsoft Corp.
#
#    Author: Tamilmani Manoharan <tamanoha@microsoft.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

"""
Setup Azure Networking
------------------
**Summary:** sets up networking required only for azure virtual machines.

This module handles any specific networking requirements for azure virtual machines (VMs).
In the present implementation, it handles moving a virtual machine from one azure virtual
network to another.

**Internal name:** ``cc_setup_azure_networking``

**Module frequency:** per instance

**Supported distros:** all
"""

from cloudinit.settings import PER_ALWAYS
import time
import os
import socket
import struct
import glob
from subprocess import Popen
from cloudinit import importer
from cloudinit import config
from cloudinit import type_utils

frequency = PER_ALWAYS

def IsAzure():
    azure_config_exists = False
    for n in glob.glob("/etc/cloud/cloud.cfg.d/*azure.cfg"):
         if os.path.isfile(n): 
             azure_config_exists=True
    return azure_config_exists

def handle(name, _cfg, _cloud, log, _args): 
    if IsAzure():
        configs_dir = os.path.dirname(os.path.abspath(__file__))
        log.debug("config dir %s", configs_dir)
        proc = Popen(["python", configs_dir + "/../sources/helpers/azuredhcpagent.py"])
        log.debug("Created netagent process")
    


  