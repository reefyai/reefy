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
sudo journalctl --list-boots
sudo journalctl -b -1
sudo journalctl -b -2 -u reefy-control
sudo journalctl --disk-usage
```

Use `journalctl --directory=/mnt/reefy-data/journal --list-boots` to inspect
the backing directory explicitly, including journals from earlier machine
identities after image or identity changes. This feature does not change
machine identity or the fleet log-publishing transport.

Persistent logging is not a crash-proof storage recorder. A boot that fails
before data mounts, a broken storage device or unsynced final entries after
power loss can still leave incomplete evidence. Logs stay on-device and
retain the existing access restrictions; this feature does not export them.

Validation should exercise early RAM entries, successful disk flush,
continued persistent writes, clean detachment, RAM fallback, and retention
across two actual reboots of a newly built image. Keep marker messages
synthetic and avoid exporting raw device journals.
