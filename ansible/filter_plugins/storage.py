"""Read-only storage validation shared by the playbook and offline tests."""

from pathlib import PurePosixPath


def storage_targets_valid(targets):
    devices = set()
    paths = []
    for target in targets:
        device = target.get("device", "")
        suffix = (
            device.removeprefix("/dev/oracleoci/oraclevd")
            if isinstance(device, str)
            else ""
        )
        allowed = list("bcdefghijklmnopqrstuvwxyz") + [
            "a" + letter for letter in "abcdef"
        ]
        if (
            not isinstance(device, str)
            or not device.startswith("/dev/oracleoci/oraclevd")
            or suffix not in allowed
        ):
            raise ValueError(
                "Storage requires an explicit OCI data-device path, never the boot disk or /dev/sdX."
            )
        if (
            target.get("attachment_type") != "paravirtualized"
            or target.get("is_read_only") is not False
        ):
            raise ValueError(
                "Only writable paravirtualized data volumes are supported by host preparation."
            )
        mount = target.get("mount_path")
        if (
            not isinstance(mount, str)
            or not mount.startswith("/")
            or mount != str(PurePosixPath(mount))
            or ".." in PurePosixPath(mount).parts
            or any(character.isspace() for character in mount)
        ):
            raise ValueError("Storage mount paths must be normalized absolute paths.")
        if not (
            mount.startswith(("/mnt/", "/var/lib/longhorn/"))
            or mount == "/var/lib/longhorn"
        ):
            raise ValueError(
                "Use /var/lib/longhorn or a dedicated directory beneath /mnt or /var/lib/longhorn."
            )
        if device in devices or any(
            mount == path
            or mount.startswith(path + "/")
            or path.startswith(mount + "/")
            for path in paths
        ):
            raise ValueError(
                "Each volume needs a distinct device and a non-overlapping mount path."
            )
        devices.add(device)
        paths.append(mount)
    return True


def storage_device_valid(
    layout, signatures, mount_path, mounted_uuid, mounted, directory_entries
):
    disks = layout.get("blockdevices", [])
    if len(disks) != 1:
        raise ValueError("Expected exactly one data disk.")
    disk = disks[0]
    if not {"name", "type", "fstype", "uuid", "mountpoints", "ro"}.issubset(
        disk
    ) or not isinstance(disk["mountpoints"], list):
        raise ValueError(
            "Disk inspection did not return all required filesystem and mount details."
        )
    if (
        disk.get("type") != "disk"
        or disk.get("ro") not in (False, 0)
        or disk.get("children")
    ):
        raise ValueError("Refusing a read-only, partitioned, or non-disk device.")
    if disk.get("fstype") not in (None, "ext4"):
        raise ValueError("Refusing to replace an existing non-ext4 filesystem.")
    detected = signatures.get("signatures")
    if not isinstance(detected, list):
        raise TypeError("Cannot establish the disk's existing signatures.")
    if any(signature.get("type") != "ext4" for signature in detected):
        raise ValueError(
            "Refusing existing partition-table, RAID, swap, or other filesystem signatures."
        )
    if bool(detected) != (disk.get("fstype") == "ext4"):
        raise ValueError(
            "Disk probes disagree about existing data; refusing to format."
        )
    if any(path not in (None, mount_path) for path in disk["mountpoints"]):
        raise ValueError("The disk is already mounted at another path.")
    if disk.get("fstype") == "ext4" and not disk.get("uuid"):
        raise ValueError("The existing ext4 filesystem has no usable UUID.")
    if mounted and (not mounted_uuid or mounted_uuid != disk.get("uuid")):
        raise ValueError("The destination is occupied by another filesystem.")
    if (mount_path in disk["mountpoints"]) != mounted:
        raise ValueError("Mount probes disagree; refusing to change storage.")
    if directory_entries:
        raise ValueError(
            "Refusing to hide files already present in the mount directory."
        )
    return True


class FilterModule:
    def filters(self):
        return {
            "storage_targets_valid": storage_targets_valid,
            "storage_device_valid": storage_device_valid,
        }
