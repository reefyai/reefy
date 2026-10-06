# Persistent boot journals

Reefy keeps the early kernel and userspace journal in `/run/log/journal`.
After `reefy-storage.service` finishes, `reefy-persistent-journal.service`
bind-mounts `/mnt/reefy-data/journal` onto `/var/log/journal` and asks
journald to flush early entries into that persistent directory. This is a
separate directory from Docker's data-root and from the protected state LV.
The service does not gate Docker or control readiness.

The normal early `systemd-journal-flush.service` is skipped until the journal
path is a mount point. Setting `Storage=persistent` alone would otherwise
write into the ephemeral root overlay and lose those files at reboot.
At shutdown the service relinquishes persistent logging before detaching
the binding. Missing, RAM-backed or read-only data storage leaves runtime
logging available. On a fresh bootstrap without persistent storage, this
boot remains volatile; retention starts on the next boot with provisioned
storage.

Retention covers multiple boots, not just the previous one. Journald rotates
and removes archived files according to the smaller of 16 GiB and 10% of available space at startup, a 256 MiB
free-space reserve and 90-day maximum age. The computed disk cap is stored in a runtime journald drop-in before disk
logging starts. It is recalculated on each boot, not continuously as logs
consume free space. The previous journald runtime size/free-space defaults remain unchanged.
No new RAM cap is imposed; the 90-day age limit is installed only when
persistent storage becomes available. These
are bounds rather than a guaranteed number of boots or days: log volume and
available space determine the history actually retained. Journald's active
files can temporarily exceed its rotation budget.

Inspect retained history locally:

```sh
sudo journalctl --directory=/mnt/reefy-data/journal --list-boots
sudo journalctl --directory=/mnt/reefy-data/journal -b -1
sudo journalctl --directory=/mnt/reefy-data/journal -b -2 -u reefy-control
sudo journalctl --directory=/mnt/reefy-data/journal --disk-usage
```

Use the backing directory explicitly for cross-boot history. On an ephemeral
root, systemd can generate a different machine ID each boot. Plain
`journalctl -b -1` then searches only the current machine identity and can
report no previous boot even when older journals are retained. The directory
reader includes those earlier identities. Keeping the existing ephemeral
machine identity is intentional: journal retention does not require a stable
machine ID, early persistent-storage mounting, or a hardware-derived ID.
Each boot has its own boot ID independently of its machine ID; the directory
reader uses boot IDs to navigate the retained history. Do not rename journal
directories or rewrite old records to combine machine identities.
This feature does not change machine identity or the fleet log-publishing
transport.

Persistent logging is not a crash-proof storage recorder. A boot that fails
before data mounts, a broken storage device or unsynced final entries after
power loss can still leave incomplete evidence. Logs stay on-device and
retain the existing access restrictions; this feature does not export them.

Validation should exercise early RAM entries, successful disk flush,
continued persistent writes, clean detachment, RAM fallback, and retention
across two actual reboots of a newly built image. The regular QEMU scenario
uses the explicit directory reader for `-b -1` and `-b -2`, checks the
markers' boot IDs and early kernel entries, and requires normal device
readiness after reboot. It does not force a stable machine ID. Keep marker
messages synthetic and avoid exporting raw device journals.
