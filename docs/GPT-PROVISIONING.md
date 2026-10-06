# Boot-disk GPT provisioning

The firmware image contains two boot partitions. When copied to a larger disk,
the backup GPT remains at the image boundary. Adoption relocates it and creates
key and, when needed, data partitions.

A GNU Parted `--fix ... print` command is a metadata write, not a read-only probe.
A controlled process interruption reproduced a window where neither GPT header
was readable. Increasing its timeout reduces the chance of interruption but does
not remove that write-order failure mode.

Use stock GPT fdisk (`sgdisk`), which writes the backup table/header before the
primary table/header. No vendor C/C++ patch is carried. The interruption
regression tests actual retained metadata and partition payloads, rather than
requiring an implementation flag such as O_SYNC. These are process-interruption
tests, not power-loss tests, and do not establish durability under device failure.

Provisioning commands have a 60-second per-command limit because this is an
infrequent operation and storage flushes can be slow. Relocation validates GPT
CRCs and partition identity before and after mutation. An uncertain relocation
is retried once only after validating the retained table; unknown or conflicting
valid tables are refused. Partition creation is not blindly retried.

## Focused timing

Three repetitions on disposable Linux loop disks expanded from a 2100 MiB image
to 32 GiB, with synthetic A/B partitions:

| Stock tool | Individual command range | Commands per provisioning sequence |
| --- | --- | --- |
| GNU Parted 3.6 | 0.021-0.036 seconds | 4 |
| GPT fdisk 1.0.10 | 1.016-1.024 seconds | 4 |

GPT fdisk's Unix sync implementation includes a one-second sleep. These
measurements did not run under the full parallel QEMU workload and do not explain
why an earlier command exceeded 15 seconds. The 60-second limit is operating
headroom, not a measured worst-case guarantee. Future full E2E results should
capture real command durations, including under contention.

Stock GPT fdisk passed interruptions at backup-header and primary-table writes
for relocation and partition creation. Existing payloads survived; conflicting
valid copies were refused. A GNU Parted interruption reproduced missing headers,
and the validator refused to rewrite that disk.

References: [upstream sgdisk documentation](https://www.rodsbooks.com/gdisk/sgdisk.html)
and GPT fdisk 1.0.10 `GPTData::SaveGPTData` / `DiskIO::DiskSync` source.
