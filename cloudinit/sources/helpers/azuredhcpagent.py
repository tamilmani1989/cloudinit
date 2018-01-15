import sys
import logging
import socket
import os
import struct
import subprocess
from logging.handlers import RotatingFileHandler

RTMGRP_LINK = 1
NLMSG_NOOP = 1
NLMSG_ERROR = 2
RTM_NEWLINK = 16
RTM_DELLINK = 17
IFLA_IFNAME = 3

def GetLogger():
    log = logging.getLogger("dhcpagent")
    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)s - %(message)s')
    
    handler = RotatingFileHandler("/var/log/dhcpagent.log", maxBytes=10485760,
                                  backupCount=5)

    handler.setFormatter(formatter)
    log.addHandler(handler)
    return log

def main():

    log = GetLogger()
    # Create the netlink socket and bind to RTMGRP_LINK,
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
    s.bind((os.getpid(), RTMGRP_LINK))

    while True:
        
        data = s.recv(65535)
        msg_len, msg_type, flags, seq, pid = struct.unpack("=LHHLL", data[:16])

        if msg_type == NLMSG_NOOP:
            log.debug("noop")
            continue
        elif msg_type == NLMSG_ERROR:
            log.debug("error")
            break

        # We fundamentally only care about NEWLINK messages in this version.
        if msg_type != RTM_NEWLINK:
            continue

        data = data[16:]

        family, _, if_type, index, flags, change = struct.unpack("=BBHiII", data[:16])

        remaining = msg_len - 32
        data = data[16:]

        while remaining:
            rta_len, rta_type = struct.unpack("=HH", data[:4])

            # This check comes from RTA_OK, and terminates a string of routing
            # attributes.
            if rta_len < 4:
                break

            rta_data = data[4:rta_len]

            increment = (rta_len + 4 - 1) & ~(4 - 1)
            data = data[increment:]
            remaining -= increment

            # Hoorah, a link is up!
            if rta_type == IFLA_IFNAME:
                log.debug("New link %s", rta_data)
                ifname = str(rta_data).strip('\0')

                operfilename = "/sys/class/net/" + ifname +"/operstate"
                operfilename = operfilename.strip("\0")
                carrierfilename = "/sys/class/net/" + ifname +"/carrier"
                carrierfilename = carrierfilename.strip("\0")

                log.debug("operfilename %s", operfilename)

                carrier=""
                operstate=""
                
                try:
                    file = open(operfilename, "r")
                    operstate = file.readline()
                    file.close()                    
                except Exception as e:
                    log.error("exception reading operstate %s", str(e))

                operstate = operstate.rstrip()
                log.debug("operstate %s", operstate)
                
                try:
                    file = open(carrierfilename, "r")
                    carrier = file.readline()
                    file.close()

                except IOError as io:
                    if io.errno != 22:
                        log.error("IO error reading carrier %s errno %d", str(io), io.errno)    
                        
                except Exception as e:
                    log.error("exception reading carrier %s", str(e))
                    

                carrier = carrier.rstrip()
                log.debug("carrier %s", carrier)
                    
                if operstate == "up" or carrier == "1":
                    log.debug("trigger dhcp")
                    return_code = subprocess.call("dhclient " + ifname, shell=True)
                    log.debug("dhclient return status %d", return_code)


if __name__ == '__main__':
    main()