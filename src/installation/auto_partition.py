#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  auto_partition.py
#
#  This file was forked from Cnchi (graphical installer from Antergos)
#  Check it at https://github.com/antergos
#
#  Copyright 2013 Antergos (http://antergos.com/)
#  Copyright 2013 Manjaro (http://manjaro.org)
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

import os
import subprocess
import logging
import show_message as show
import parted3.partition_module as pm
import parted3.fs_module as fs
import parted3.lvm as lvm
import parted3.used_space as used_space

""" AutoPartition class """

# Partition sizes are in MB
MAX_ROOT_SIZE = 30000

# TODO: This higly depends on the selected DE! Must be taken into account.
MIN_ROOT_SIZE = 6000


def check_output(command):
    """ Calls subprocess.check_output, decodes its exit and removes trailing \n """
    return subprocess.check_output(command.split()).decode().strip("\n")


def printk(enable):
    """ Enables / disables printing kernel messages to console """
    with open("/proc/sys/kernel/printk", "w") as fpk:
        if enable:
            fpk.write("4")
        else:
            fpk.write("0")


def unmount_all(dest_dir):
    """ Unmounts all devices that are mounted inside dest_dir """
    subprocess.check_call(["swapoff", "-a"])

    mount_result = subprocess.check_output("mount").decode().split("\n")

    # Umount all devices mounted inside dest_dir (if any)
    dirs = []
    for mount in mount_result:
        if dest_dir in mount:
            directory = mount.split()[0]
            # Do not unmount dest_dir now (we will do it later)
            if directory is not dest_dir:
                dirs.append(directory)

    for directory in dirs:
        logging.warning(_("Unmounting %s"), directory)
        try:
            subprocess.call(["umount", directory])
        except Exception:
            logging.warning(_("Unmounting %s failed. Trying lazy arg."), directory)
            subprocess.call(["umount", "-l", directory])

    # Now is the time to unmount the device that is mounted in dest_dir (if any)

    if dest_dir in mount_result:
        logging.warning(_("Unmounting %s"), dest_dir)
        try:
            subprocess.call(["umount", directory])
        except Exception:
            logging.warning(_("Unmounting %s failed. Trying lazy arg."), directory)
            subprocess.call(["umount", "-l", directory])

    # Remove all previous Manjaro LVM volumes
    # (it may have been left created due to a previous failed installation)
    try:
        if os.path.exists("/dev/mapper/ManjaroRoot"):
            subprocess.check_call(["lvremove", "-f", "/dev/mapper/ManjaroRoot"])
        if os.path.exists("/dev/mapper/ManjaroSwap"):
            subprocess.check_call(["lvremove", "-f", "/dev/mapper/ManjaroSwap"])
        if os.path.exists("/dev/mapper/ManjaroHome"):
            subprocess.check_call(["lvremove", "-f", "/dev/mapper/ManjaroHome"])
        if os.path.exists("/dev/ManjaroVG"):
            subprocess.check_call(["vgremove", "-f", "ManjaroVG"])
        pvolumes = check_output("pvs -o pv_name --noheading").split("\n")
        if len(pvolumes[0]) > 0:
            for pvolume in pvolumes:
                pvolume = pvolume.strip(" ")
                subprocess.check_call(["pvremove", "-f", pvolume])
    except subprocess.CalledProcessError as err:
        logging.warning(_("Can't delete existent LVM volumes (see below)"))
        logging.warning(err)

    # Close LUKS devices (they may have been left open because of a previous failed installation)
    try:
        if os.path.exists("/dev/mapper/cryptManjaro"):
            subprocess.check_call(["cryptsetup", "luksClose", "/dev/mapper/cryptManjaro"])
        if os.path.exists("/dev/mapper/cryptManjaroHome"):
            subprocess.check_call(["cryptsetup", "luksClose", "/dev/mapper/cryptManjaroHome"])
    except subprocess.CalledProcessError as err:
        logging.warning(_("Can't close LUKS devices (see below)"))
        logging.warning(err)


class AutoPartition(object):
    """ Class used by the automatic installation method """
    def __init__(self, dest_dir, auto_device, use_luks, use_lvm, luks_key_pass, use_home, callback_queue):
        """ Class initialization """
        self.dest_dir = dest_dir
        self.auto_device = auto_device
        self.luks_key_pass = luks_key_pass
        # Use LUKS encryption
        self.luks = use_luks
        # Use LVM
        self.lvm = use_lvm
        # Make home a different partition or if using LVM, a different volume
        self.home = use_home

        # Will use these queue to show progress info to the user
        self.callback_queue = callback_queue

        self.efi = False
        if os.path.exists("/sys/firmware/efi"):
            self.efi = True

    def mkfs(self, device, fs_type, mount_point, label_name, fs_options="", btrfs_devices=""):
        """ We have two main cases: "swap" and everything else. """
        if fs_type == "swap":
            try:
                swap_devices = check_output("swapon -s")
                if device in swap_devices:
                    subprocess.check_call(["swapoff", device])
                subprocess.check_call(["mkswap", "-L", label_name, device])
                subprocess.check_call(["swapon", device])
            except subprocess.CalledProcessError as err:
                logging.warning(err.output)
        else:
            mkfs = {"xfs": "mkfs.xfs %s -L %s -f %s" % (fs_options, label_name, device),
                    "jfs": "yes | mkfs.jfs %s -L %s %s" % (fs_options, label_name, device),
                    "reiserfs": "yes | mkreiserfs %s -l %s %s" % (fs_options, label_name, device),
                    "ext2": "mkfs.ext2 -q -L %s %s %s" % (fs_options, label_name, device),
                    "ext3": "mke2fs -q %s -L %s -t ext3 %s" % (fs_options, label_name, device),
                    "ext4": "mke2fs -q %s -L %s -t ext4 %s" % (fs_options, label_name, device),
                    "btrfs": "mkfs.btrfs %s -L %s %s" % (fs_options, label_name, btrfs_devices),
                    "nilfs2": "mkfs.nilfs2 %s -L %s %s" % (fs_options, label_name, device),
                    "ntfs-3g": "mkfs.ntfs %s -L %s %s" % (fs_options, label_name, device),
                    "vfat": "mkfs.vfat %s -n %s %s" % (fs_options, label_name, device)}

            # Make sure the fs type is one we can handle
            if fs_type not in mkfs.keys():
                txt = _("Unkown filesystem type %s"), fs_type
                logging.error(txt)
                show.error(txt)
                return

            command = mkfs[fs_type]

            try:
                subprocess.check_call(command.split())
            except subprocess.CalledProcessError as err:
                txt = _("Can't create filesystem %s") % fs_type
                logging.error(txt)
                logging.error(err.cmd)
                logging.error(err.output)
                show.error(txt)
                return

            # Flush filesystem buffers
            subprocess.check_call(["sync"])

            # Create our mount directory
            path = self.dest_dir + mount_point
            subprocess.check_call(["mkdir", "-p", path])

            # Mount our new filesystem
            subprocess.check_call(["mount", "-t", fs_type, device, path])

            # Change permission of base directories to avoid btrfs issues
            mode = "755"

            if mount_point == "/tmp":
                mode = "1777"
            elif mount_point == "/root":
                mode = "750"

            subprocess.check_call(["chmod", mode, path])

        fs_uuid = fs.get_info(device)['UUID']
        fs_label = fs.get_info(device)['LABEL']
        logging.debug("Device details: %s UUID=%s LABEL=%s", device, fs_uuid, fs_label)

    def get_devices(self):
        """ Set (and return) all partitions on the device """
        efi = ""
        boot = ""
        swap = ""
        root = ""
        home = ""

        luks = []
        lvm = ""

        # self.auto_device is of type /dev/sdX or /dev/hdX

        if self.efi:
            efi = self.auto_device + "2"
            boot = self.auto_device + "3"
            swap = self.auto_device + "4"
            root = self.auto_device + "5"
            if self.home:
                home = self.auto_device + "6"
        else:
            boot = self.auto_device + "1"
            swap = self.auto_device + "2"
            root = self.auto_device + "3"
            if self.home:
                home = self.auto_device + "4"

        if self.luks:
            if self.lvm:
                # LUKS and LVM
                luks = [swap]
                lvm = "/dev/mapper/cryptManjaro"
            else:
                # LUKS and no LVM
                luks = [root]
                root = "/dev/mapper/cryptManjaro"
                if self.home:
                    # In this case we'll have two LUKS devices, one for root
                    # and the other one for /home
                    luks.append(home)
                    home = "/dev/mapper/cryptManjaroHome"
        elif self.lvm:
            # No LUKS but using LVM
            lvm = swap

        if self.lvm:
            swap = "/dev/ManjaroVG/ManjaroSwap"
            root = "/dev/ManjaroVG/ManjaroRoot"
            if self.home:
                home = "/dev/ManjaroVG/ManjaroHome"

        return (efi, boot, swap, root, luks, lvm, home)

    def get_mount_devices(self):
        """ Mount_devices will be used when configuring GRUB in modify_grub_default() in installation_process.py """

        (efi_device, boot_device, swap_device, root_device, luks_devices, lvm_device, home_device) = self.get_devices()

        mount_devices = {}
        mount_devices["/boot"] = boot_device
        mount_devices["/"] = root_device
        mount_devices["/home"] = home_device

        if self.efi:
            mount_devices["/boot/efi"] = efi_device

        if self.luks:
            mount_devices["/"] = luks_devices[0]
            if self.home and not self.lvm:
                mount_devices["/home"] = luks_devices[1]

        mount_devices["swap"] = swap_device

        for md in mount_devices:
            logging.debug("mount_devices[%s] = %s", md, mount_devices[md])

        return mount_devices

    def get_fs_devices(self):
        """ fs_devices will be used when configuring the fstab file in installation_process.py """

        (efi_device, boot_device, swap_device, root_device, luks_devices, lvm_device, home_device) = self.get_devices()

        fs_devices = {}

        fs_devices[boot_device] = "ext2"
        fs_devices[swap_device] = "swap"

        if self.efi:
            fs_devices[efi_device] = "vfat"

        if self.luks:
            fs_devices[luks_devices[0]] = "ext4"
            if self.home:
                if self.lvm:
                    # luks, lvm, home
                    fs_devices[home_device] = "ext4"
                else:
                    # luks, home
                    fs_devices[luks_devices[1]] = "ext4"
        else:
            fs_devices[root_device] = "ext4"
            if self.home:
                fs_devices[home_device] = "ext4"

        for f in fs_devices:
            logging.debug("fs_devices[%s] = %s", f, fs_devices[f])

        return fs_devices

    def setup_luks(self, luks_device, luks_name, key_file):
        """ Setups a luks device """
        # For now, we we'll use the same password for root and /home
        # If instead user wants to use a key file, we'll have two different key files.

        logging.debug(_("Thus will setup LUKS on device %s"), luks_device)

        # Wipe LUKS header (just in case we're installing on a pre LUKS setup)
        # For 512 bit key length the header is 2MB
        # If in doubt, just be generous and overwrite the first 10MB or so
        subprocess.check_call(["dd", "if=/dev/zero", "of=%s" % luks_device, "bs=512", "count=20480", "status=noxfer"])

        if self.luks_key_pass == "":
            # No key password given, let's create a random keyfile
            subprocess.check_call(["dd", "if=/dev/urandom", "of=%s" % key_file, "bs=1024", "count=4", "status=noxfer"])

            # Set up luks with a keyfile
            subprocess.check_call(["cryptsetup", "luksFormat", "-q", "-c", "aes-xts-plain", "-s", "512",
                luks_device, key_file])
            subprocess.check_call(["cryptsetup", "luksOpen", luks_device, luks_name, "-q", "--key-file",
                key_file])
        else:
            # Set up luks with a password key
            luks_key_pass_bytes = bytes(self.luks_key_pass, 'UTF-8')

            proc = subprocess.Popen(["cryptsetup", "luksFormat", "-q", "-c", "aes-xts-plain", "-s", "512",
                "--key-file=-", luks_device], stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
            (stdout_data, stderr_data) = proc.communicate(input=luks_key_pass_bytes)

            proc = subprocess.Popen(["cryptsetup", "luksOpen", luks_device, luks_name, "-q", "--key-file=-"],
                stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
            (stdout_data, stderr_data) = proc.communicate(input=luks_key_pass_bytes)

    def run(self):
        key_files = ["/tmp/.keyfile-root", "/tmp/.keyfile-home"]

        # Partition sizes are expressed in MB
        if self.efi:
            gpt_bios_grub_part_size = 2
            efisys_part_size = 100
            empty_space_size = 2
        else:
            gpt_bios_grub_part_size = 0
            efisys_part_size = 0
            empty_space_size = 0

        boot_part_size = 200

        # Get just the disk size in 1000*1000 MB
        device = self.auto_device
        device_name = check_output("basename %s" % device)
        base_path = "/sys/block/%s" % device_name
        disk_size = 0
        if os.path.exists("%s/size" % base_path):
            with open("%s/queue/logical_block_size" % base_path, 'r') as f:
                logical_block_size = int(f.read())
            with open("%s/size" % base_path, 'r') as f:
                size = int(f.read())

            disk_size = ((logical_block_size * size) / 1024) / 1024
        else:
            txt = _("Setup cannot detect size of your device, please use advanced "
                "installation routine for partitioning and mounting devices.")
            logging.error(txt)
            show.warning(txt)
            return

        mem_total = check_output("grep MemTotal /proc/meminfo")
        mem_total = int(mem_total.split()[1])
        mem = mem_total / 1024

        # Suggested sizes from Anaconda installer
        if mem < 2048:
            swap_part_size = 2 * mem
        elif 2048 <= mem < 8192:
            swap_part_size = mem
        elif 8192 <= mem < 65536:
            swap_part_size = mem / 2
        else:
            swap_part_size = 4096

        # Max swap size is 10% of all available disk size
        max_swap = disk_size * 0.1
        if swap_part_size > max_swap:
            swap_part_size = max_swap

        root_part_size = disk_size - (empty_space_size + gpt_bios_grub_part_size + efisys_part_size + boot_part_size + swap_part_size)

        home_part_size = 0
        if self.home:
            # Decide how much we leave to root and how much we leave to /home
            new_root_part_size = root_part_size / 5
            if new_root_part_size > MAX_ROOT_SIZE:
                new_root_part_size = MAX_ROOT_SIZE
            elif new_root_part_size < MIN_ROOT_SIZE:
                new_root_part_size = MIN_ROOT_SIZE
            home_part_size = root_part_size - new_root_part_size
            root_part_size = new_root_part_size

        lvm_pv_part_size = swap_part_size + root_part_size + home_part_size

        logging.debug("disk_size %dMB", disk_size)
        logging.debug("gpt_bios_grub_part_size %dMB", gpt_bios_grub_part_size)
        logging.debug("efisys_part_size %dMB", efisys_part_size)
        logging.debug("boot_part_size %dMB", boot_part_size)

        if self.lvm:
            logging.debug("lvm_pv_part_size %dMB", lvm_pv_part_size)

        logging.debug("swap_part_size %dMB", swap_part_size)
        logging.debug("root_part_size %dMB", root_part_size)

        if self.home:
            logging.debug("home_part_size %dMB", home_part_size)

        # Disable swap and all mounted partitions, umount / last!
        unmount_all(self.dest_dir)

        printk(False)

        # We assume a /dev/hdX format (or /dev/sdX)
        if self.efi:
            # GPT (GUID) is supported only by 'parted' or 'sgdisk'
            # clean partition table to avoid issues!
            subprocess.check_call(["sgdisk", "--zap", device])

            # Clear all magic strings/signatures - mdadm, lvm, partition tables etc.
            subprocess.check_call(["dd", "if=/dev/zero", "of=%s" % device, "bs=512", "count=2048", "status=noxfer"])
            subprocess.check_call(["wipefs", "-a", device])
            # Create fresh GPT
            subprocess.check_call(["sgdisk", "--clear", device])
            # Inform the kernel of the partition change. Needed if the hard disk had a MBR partition table.
            subprocess.check_call(["partprobe", device])
            # Create actual partitions
            subprocess.check_call(['sgdisk --set-alignment="2048" --new=1:1M:+%dM --typecode=1:EF02 --change-name=1:BIOS_GRUB %s'
                % (gpt_bios_grub_part_size, device)], shell=True)
            subprocess.check_call(['sgdisk --set-alignment="2048" --new=2:0:+%dM --typecode=2:EF00 --change-name=2:UEFI_SYSTEM %s'
                % (efisys_part_size, device)], shell=True)
            subprocess.check_call(['sgdisk --set-alignment="2048" --new=3:0:+%dM --typecode=3:8300 --attributes=3:set:2 --change-name=3:MANJARO_BOOT %s'
                % (boot_part_size, device)], shell=True)

            if self.lvm:
                subprocess.check_call(['sgdisk --set-alignment="2048" --new=4:0:+%dM --typecode=4:8E00 --change-name=4:MANJARO_LVM %s'
                    % (lvm_pv_part_size, device)], shell=True)
            else:
                subprocess.check_call(['sgdisk --set-alignment="2048" --new=4:0:+%dM --typecode=4:8200 --change-name=4:MANJARO_SWAP %s'
                    % (swap_part_size, device)], shell=True)
                subprocess.check_call(['sgdisk --set-alignment="2048" --new=5:0:+%dM --typecode=5:8300 --change-name=5:MANJARO_ROOT %s'
                    % (root_part_size, device)], shell=True)

                if self.home:
                    subprocess.check_call(['sgdisk --set-alignment="2048" --new=6:0:+%dM --typecode=6:8300 --change-name=5:MANJARO_HOME %s'
                        % (home_part_size, device)], shell=True)

            logging.debug(check_output("sgdisk --print %s" % device))
        else:
            # Start at sector 1 for 4k drive compatibility and correct alignment
            # Clean partitiontable to avoid issues!
            subprocess.check_call(["dd", "if=/dev/zero", "of=%s" % device, "bs=512", "count=2048", "status=noxfer"])
            subprocess.check_call(["wipefs", "-a", device])

            # Create DOS MBR with parted
            subprocess.check_call(["parted", "-a", "optimal", "-s", device, "mktable", "msdos"])

            # Create boot partition (all sizes are in MB)
            subprocess.check_call(["parted", "-a", "optimal", "-s", device, "mkpart", "primary", "1", str(boot_part_size)])
            # Set boot partition as bootable
            subprocess.check_call(["parted", "-a", "optimal", "-s", device, "set", "1", "boot", "on"])

            if self.lvm:
                start = boot_part_size
                end = start + lvm_pv_part_size
                # Create partition for lvm (will store root, swap and home (if desired) logical volumes)
                subprocess.check_call(["parted", "-a", "optimal", "-s", device, "mkpart", "primary", str(start), "100%"])
                # Set lvm flag
                subprocess.check_call(["parted", "-a", "optimal", "-s", device, "set", "2", "lvm", "on"])
            else:
                # Create swap partition
                start = boot_part_size
                end = start + swap_part_size
                subprocess.check_call(["parted", "-a", "optimal", "-s", device, "mkpart", "primary", "linux-swap",
                    str(start), str(end)])

                # Create root partition
                start = end
                end = start + root_part_size
                subprocess.check_call(["parted", "-a", "optimal", "-s", device, "mkpart", "primary",
                    str(start), str(end)])

                if self.home:
                    # Create home partition
                    start = end
                    subprocess.check_call(["parted", "-a", "optimal", "-s", device, "mkpart", "primary",
                        str(start), "100%"])

        printk(True)

        # Wait until /dev initialized correct devices
        subprocess.check_call(["udevadm", "settle", "--quiet"])

        (efi_device, boot_device, swap_device, root_device, luks_devices, lvm_device, home_device) = self.get_devices()

        if not self.home and self.efi:
            logging.debug("EFI %s, Boot %s, Swap %s, Root %s", efi_device, boot_device, swap_device, root_device)
        elif not self.home and not self.efi:
            logging.debug("Boot %s, Swap %s, Root %s", boot_device, swap_device, root_device)
        elif self.home and self.efi:
            logging.debug("EFI %s, Boot %s, Swap %s, Root %s, Home %s", efi_device, boot_device, swap_device, root_device, home_device)
        else:
            logging.debug("Boot %s, Swap %s, Root %s, Home %s", boot_device, swap_device, root_device, home_device)

        if self.luks:
            self.setup_luks(luks_devices[0], "cryptManjaro", key_files[0])
            if self.home and not self.lvm:
                self.setup_luks(luks_devices[1], "cryptManjaroHome", key_files[1])

        if self.lvm:
            logging.debug(_("Will setup LVM on device %s"), lvm_device)

            subprocess.check_call(["pvcreate", "-f", "-y", lvm_device])
            subprocess.check_call(["vgcreate", "-f", "-y", "ManjaroVG", lvm_device])

            subprocess.check_call(["lvcreate", "--name", "ManjaroRoot", "--size", str(int(root_part_size)), "ManjaroVG"])

            if not self.home:
                # Use the remainig space for our swap volume
                subprocess.check_call(["lvcreate", "--name", "ManjaroSwap", "--extents", "100%FREE", "ManjaroVG"])
            else:
                subprocess.check_call(["lvcreate", "--name", "ManjaroSwap", "--size", str(int(swap_part_size)), "ManjaroVG"])
                # Use the remainig space for our home volume
                subprocess.check_call(["lvcreate", "--name", "ManjaroHome", "--extents", "100%FREE", "ManjaroVG"])

        # Make sure the "root" partition is defined first!
        self.mkfs(root_device, "ext4", "/", "ManjaroRoot")
        self.mkfs(swap_device, "swap", "", "ManjaroSwap")
        self.mkfs(boot_device, "ext2", "/boot", "ManjaroBoot")

        # Format the EFI partition
        if self.efi:
            self.mkfs(efi_device, "vfat", "/boot/efi", "UEFI_SYSTEM", "-F 32")

        if self.home:
            self.mkfs(home_device, "ext4", "/home", "ManjaroHome")

        # NOTE: encrypted and/or lvm2 hooks will be added to mkinitcpio.conf in installation_process.py if necessary
        # NOTE: /etc/default/grub, /etc/stab and /etc/crypttab will be modified in installation_process.py, too.

        if self.luks and self.luks_key_pass == "":
            # Copy root keyfile to boot partition and home keyfile to root partition
            # user will choose what to do with it
            # THIS IS NONSENSE (BIG SECURITY HOLE), BUT WE TRUST THE USER TO FIX THIS
            # User shouldn't store the keyfiles unencrypted unless the medium itself is reasonably safe
            # (boot partition is not)
            subprocess.check_call(['chmod', '0400', key_files[0]])
            subprocess.check_call(['mv', key_files[0], '%s/boot' % self.dest_dir])
            if self.home and not self.lvm:
                subprocess.check_call(['chmod', '0400', key_files[1]])
                subprocess.check_call(["mkdir", "-p", '%s/etc/luks-keys' % self.dest_dir])
                subprocess.check_call(['mv', key_files[1], '%s/etc/luks-keys' % self.dest_dir])

if __name__ == '__main__':
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    ap = AutoPartition("/install",
        "/dev/sdb",
        use_luks=False,
        use_lvm=True,
        luks_key_pass="",
        use_home=True,
        callback_queue=None)

#    ap.run()
