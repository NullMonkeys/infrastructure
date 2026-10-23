# Host preparation

[`ansible/playbooks/prepare.yml`](../ansible/playbooks/prepare.yml) prepares Ubuntu
hosts after Terraform deployment and inventory generation. It waits for SSH and
cloud-init, disables IPv6, installs packages, and mounts attached data storage.
It does not install K3s or Longhorn or modify IPv4 routing/firewall rules.

## IPv4-only hosts

Host preparation enforces `ipv6.disable=1` on the kernel command line using
`/etc/default/grub.d/99-disable-ipv6.cfg`, then runs `update-grub`. The drop-in
preserves other boot options and replaces conflicting `ipv6.disable` settings.
This disables IPv6 at kernel level, including loopback, future interfaces, and
IPv6 sockets, rather than only disabling address configuration on existing NICs.

The playbook configures one host at a time. A host not already booted with the
setting is automatically rebooted; Ansible waits for it to return and verifies the
effective boot flag and that creating an IPv6 socket fails. It then continues
with packages and storage. Subsequent runs verify the setting without another
reboot when it is already active. The deployment lock covers the entire process.

These tasks run before package installation. `--check` does not rebuild GRUB or
reboot hosts. The image must use Ubuntu's GRUB configuration; images without
`/etc/default/grub` fail before changing boot files. With kernel-level disabling, `net.ipv6.*`
sysctl keys can be absent; the playbook verifies actual socket availability.
K3s and future Longhorn configuration must retain this IPv4-only policy.

## Packages

The `node` role installs `ca-certificates`, `curl`, `jq`, `e2fsprogs`, `util-linux`,
`kmod`, `open-iscsi`, and `nfs-common`. It starts/enables `iscsid` and loads
`iscsi_tcp` persistently. The iSCSI and NFS packages prepare for Longhorn's V1 engine
and RWX volumes; selecting a Longhorn release and checking its remaining cluster
prerequisites belongs to the installation step.

Override `node_packages` to adjust the baseline. Defaults live in
[`ansible/roles/node/defaults/main.yml`](../ansible/roles/node/defaults/main.yml).
Collections are pinned in `ansible/collections/requirements.yml` and installed
separately from the Python requirements.

## Storage ownership and prerequisites

Terraform creates and attaches OCI block volumes. Ansible formats and mounts the
guest devices; it does not create a second cloud attachment. The `inventory` output
now includes `block_volumes` per host, taken from the actual volume attachments.
All workspaces must apply these outputs before preparation can run.

Automatic preparation currently supports writable **paravirtualized** attachments
with OCI consistent device paths. When a node has one paravirtualized data volume,
Terraform sets its attachment device to `/dev/oracleoci/oraclevdb` by default.
An explicit `device` overrides that default. No HCP input change is needed for the
single-volume case, for example:

```hcl
block_volumes = {
  data = {
    size_in_gbs = 50
  }
}
```

When a node has multiple volumes, set an explicit, distinct `device` on every
volume (for example `/dev/oracleoci/oraclevdb` and `/dev/oracleoci/oraclevdc`). Pin
the existing volume's path when adding another volume; paths are never assigned
by map order. Missing or duplicate paths fail the Terraform plan in this case.

Changing an attachment device, including adopting the default for an existing
attachment without a device path, replaces **the attachment**: Terraform detaches
and reattaches the same block volume to the same instance. It does not recreate the
volume or instance, but access is interrupted during reattachment. Review that
one-time plan before deployment. The image must support OCI consistent device paths.
Ansible still refuses missing/boot paths, read-only attachments, and unsupported
attachment modes; it never guesses devices from disk order or size.

One data volume per host defaults to ext4 at `/var/lib/longhorn`. Nodes without
data volumes receive IPv4-only configuration and packages; the role does not
substitute the boot disk.
For multiple volumes, provide an explicit, distinct `storage_mounts` entry for
each volume, for example through Ansible extra vars:

```yaml
storage_mounts:
  data: /var/lib/longhorn
  extra: /mnt/extra
```

Paths must be `/var/lib/longhorn` or a dedicated directory beneath it or `/mnt`.
Nested or duplicate mounts and symbolic-link redirects are rejected. Additional
disks will need explicit configuration in Longhorn when it is installed later.

Before formatting, the role inspects `lsblk`, `wipefs`, the active mount, and the
destination directory. It rejects partitioned/read-only devices, recognized
non-ext4 signatures, conflicting mounts, nonempty unmounted directories, and
inconsistent probe results. Only a whole disk without detected signatures is
formatted. Existing ext4 is reused without reformatting. Disks containing raw data
without recognizable signatures must not be supplied for automatic initialization.

Mounts use filesystem UUIDs and `defaults,_netdev,nofail` in `/etc/fstab`, with a
backup of any changed fstab. Repeated runs preserve existing filesystems and mounts.
Removing a volume from inputs does not clean up old fstab entries or migrate data;
storage removal/replacement requires its own migration.
The K3s role adds a `RequiresMountsFor` service dependency for prepared data mounts,
so a failed mount blocks startup rather than redirecting future storage writes onto
the boot filesystem. Preserve this dependency when installing Longhorn.

## Execution

The Deployment workflow runs host preparation after inventory succeeds and holds
its concurrency lock through preparation, K3s installation, and cluster verification.
See [K3s setup](k3s.md). It uses repository secret
`ANSIBLE_SSH_PRIVATE_KEY` and optional variable `ANSIBLE_REMOTE_USER` (default
`ubuntu`). The remote user must have passwordless sudo. SSH keys and known-hosts
files exist only in the runner's temporary directory and are cleaned up afterward.

For an authorized local run, after installing dependencies and generating inventory:

```sh
ANSIBLE_CONFIG=ansible/ansible.cfg .venv/bin/ansible-playbook \
  --user ubuntu --private-key /path/to/private-key ansible/playbooks/prepare.yml
```

`--check` contacts hosts but does not reboot, format/mount disks, or start services. On a
fresh image it can fail if inspection tools are not installed yet; a blank disk's
UUID and final mount cannot be predicted until it is formatted.

References: [Longhorn prerequisites](https://longhorn.io/docs/1.13.0/deploy/install/),
[OCI device paths](https://docs.oracle.com/en-us/iaas/Content/Block/References/consistentdevicepaths.htm),
[OCI fstab options](https://docs.oracle.com/en-us/iaas/Content/Block/References/fstaboptionsconsistentdevicepaths.htm),
[Linux IPv6 disabling](https://docs.kernel.org/networking/ipv6.html).
