# Crash dumps on Reefy OS

Reefy enables the stock systemd-coredump handler and coredumpctl. Native systemd
records crash metadata in journald and stores compressed userspace ELF cores.
No vendor source patch or automatic upload is used. Crash dumps remain on the
customer's device; the customer or their authorized administrator controls
whether to export and share them. This captures userspace
fatal signals, not kernel panics, firmware faults or power loss.

Before persistent storage is available, files are stored in a private 256 MiB
RAM filesystem. Native retention is 128 MiB with a 64 MiB maximum uncompressed
core. Once a writable supported data filesystem is mounted, the canonical
`/var/lib/systemd/coredump` path binds to `/mnt/reefy-data/coredumps`, outside
Docker's data root and the protected configuration state volume. Retention is
14 days. The pool budget is the smaller of 4 GiB and 5% of available space at
attachment, with 512 MiB kept free. Individual uncompressed cores are limited
to the smaller of 2 GiB and half the pool budget. These native size/free-space
policies are not a filesystem quota and can temporarily overshoot during capture.
Oversized cores may be truncated and unsuitable for complete analysis.

Completed early dumps are copied with their metadata, synced and published
before their RAM source is removed. The RAM mount stays available to early
workers in private namespaces; a timer collects late-completing files and
retries attachment after recovery/adoption. An interrupted or failed transfer
retains its source. Unrelated mounts and symlink crash directories are refused.
Capture/attachment failures do not block normal boot or storage provisioning.

If the persistent filesystem fails to mount, dumps remain bounded in RAM.
They cannot survive a reboot while no durable destination is available. Inspect
or explicitly export that evidence before rebooting. Cores can contain process
memory and credentials; directories are mode 0700 and access is restricted to
root. Existing per-service core limits and Linux dumpability restrictions can
still prevent a particular process from being captured. No automatic export is
performed. Automatic stack unwinding is disabled to keep capture overhead low.

For current-boot metadata:

```sh
sudo coredumpctl list
sudo coredumpctl info
```

For retained history across changing machine IDs:

```sh
sudo coredumpctl --directory=/mnt/reefy-data/journal list
sudo coredumpctl --directory=/mnt/reefy-data/journal info
sudo coredumpctl --directory=/mnt/reefy-data/journal \
  --output=/tmp/synthetic.core dump COREDUMP_UNIT=synthetic-crash.service
```

Use the matching executable, shared libraries and available debug symbols when
analyzing a core. Kernel/module debug archives alone do not provide userspace
symbols. Native metadata and the retained boot journal identify the crashed
executable and boot; verify the corresponding firmware before debugging.

References: [systemd-coredump](https://www.freedesktop.org/software/systemd/man/latest/systemd-coredump.html)
and [coredump.conf](https://www.freedesktop.org/software/systemd/man/latest/coredump.conf.html).

## Memory capture policy

Systemd-managed services default to `CoredumpFilter=elf-headers`. Linux omits
anonymous heap and stack mappings and ordinary mapped-file contents. Core
register notes, mapping metadata, ELF header pages and the vDSO remain. This
reduces dump size and retained application data; it does not anonymize core
metadata or registers. The stock collector also stores process command-line
and environment metadata in the journal independently of the memory filter.
These records remain private diagnostic data and must not be published or
uploaded automatically. Keep the private access and retention limits in place.
See [systemd-coredump metadata](https://www.freedesktop.org/software/systemd/man/latest/systemd-coredump.html#COREDUMP_ENVIRON=).

Matching symbols can resolve the faulting instruction to a function and source
line, but missing stack memory normally prevents a complete backtrace. For an
explicit diagnostic session, the customer or their authorized administrator can override one unit with
`CoredumpFilter=default` in its own service drop-in. That captures private memory
and may retain secrets. The QEMU source-backtrace test uses this fuller profile
only for a synthetic crash process; default-profile coverage separately checks
that a synthetic heap payload is absent.

Kernel mapping-filter semantics are documented in
[core(5)](https://man7.org/linux/man-pages/man5/core.5.html), and the per-service
setting in [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#CoredumpFilter=).

For symbol collection, evidence matching, access controls and trust limitations,
see [diagnostic traceability](DEBUGGING-TRUST.md).
