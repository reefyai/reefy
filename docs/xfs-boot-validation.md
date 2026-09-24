# Primary XFS validation and diagnostics

Mounting XFS replays its journal; it does not check all inode metadata.
The primary `reefy_default` filesystem is therefore checked before storage
startup completes, even when its first mount succeeds. The boot sequence is:

1. Mount to replay the journal, then unmount before consumers start.
2. Run `xfs_repair -n` on the unmounted filesystem.
3. If the scan completed and reported corruption, run `xfs_repair` once.
4. Require a clean independent `xfs_repair -n` pass, then mount for services.

A failed or incomplete scan does not authorize repair. A failed unmount,
unrecognized filesystem, volume mounted elsewhere, or unrelated mount
failure also refuses recovery. Log reset remains restricted to the existing
case where kernel mount rejected corrupt metadata and normal repair then
reported an unreplayable log. A successful journal replay never authorizes
`-L`. Failed validation leaves control available for bootstrap diagnostics
and blocks provisioning and the reconciler through the existing failure guard.

This checks metadata, not file contents. Repair can remove entries whose
inodes have been destroyed. It cannot recreate their data or prove that the
hardware is reliable. The synthetic regression deliberately destroys 24
inodes and requires unaffected file contents and the resulting namespace to
remain usable, rather than claiming that all files were recovered.

The check runs on every boot for the primary XFS filesystem. Its cost depends
on metadata population and device speed, not just the virtual LV size. The
small synthetic fixture is not a timing estimate for a populated device.
The storage recovery stage has a three-hour availability backstop, described
in [storage recovery backstop](storage-recovery-backstop.md). Existing A/B
confirmation and rollback budgets exclude the period when storage is activating.
A timeout enters diagnostics and prevents automatic rollback from interrupting
that session; it never confirms a failed firmware slot.
This change does not introduce exhaustive checks for state and application
filesystems; their existing mount and repair behavior remains separate.

## Online diagnostics in the next kernel

The pinned kernel supports `CONFIG_XFS_ONLINE_SCRUB` independently of
`CONFIG_XFS_ONLINE_REPAIR`. Both capabilities and scrub statistics are enabled. This provides online
repair for future operator use; it does not schedule or invoke repairs. `xfs_scrub -n /mountpoint` can then inspect
mounted filesystems without requesting repair or optimization. Statistics
are available under debugfs at `/sys/kernel/debug/xfs/scrub`.
`CONFIG_IKCONFIG` and `CONFIG_IKCONFIG_PROC` expose the built configuration
through `/proc/config.gz`, making diagnostic feature availability verifiable.

No automatic scrub timer, filesystem reformat, reverse-mapping migration,
automatic online repair, or XFS debug build is introduced. Existing pstore support
does not by itself provide persistent crash logs: its backing storage needs
separate configuration and validation.

References: the pinned [XFS Kconfig](https://github.com/gregkh/linux/blob/v6.18.52/fs/xfs/Kconfig),
upstream [xfs_repair manual](https://www.man7.org/linux/man-pages/man8/xfs_repair.8.html),
and [xfs_scrub manual](https://www.man7.org/linux/man-pages/man8/xfs_scrub.8.html).
