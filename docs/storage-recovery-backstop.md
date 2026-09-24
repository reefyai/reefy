# Storage recovery availability backstop

After mounting the boot ESP and establishing EFI entries, boot supervises the
storage discovery, check, repair and mount stage with a three-hour monotonic
wall-clock budget. This applies on ordinary boots and pending A/B updates.
Boot confirmation also has `TimeoutStartSec=3h`, measured from its own start.
Expiry refuses confirmation; it does not kill the storage unit. Storage
cleanup may therefore finish after confirmation has already timed out.
Three hours is an availability policy, not a measured upper bound or proof
that a repair is stuck. Legitimate operations can exceed it. ESP discovery
and EFI entry setup precede this budget. Application-volume repair remains
separate and retains its existing limits.

At expiry the supervisor:

1. Sets the runtime storage failure guard, inhibiting provisioning, reconciliation
   and A/B rollback. It does not confirm the firmware slot.
2. Saves at most 64 KiB of first-time evidence to
   `/mnt/reefy/reefy/recovery/storage-timeout.json`: elapsed time, process states,
   wait channels, I/O counters and recent storage journal messages. The report
   and directory are fsynced and the ESP remounted read-only. A partial record
   also inhibits future automatic recovery.
3. Sends SIGINT to the recovery process group and allows 60 seconds to exit,
   then SIGTERM with another 60 seconds. It checks surviving descendants,
   not only the original shell. These grace periods allow processes to react
   to signals; they are not repair completion estimates.
4. If recovery stopped and all data mounts can be normally unmounted, fails
   the storage unit and lets bootstrap control/MQTT diagnostics start.
5. If recovery or unmount remains stuck, reboots only after successful marker
   persistence and verified EFI BootNext selection of the current firmware.
   The final reset uses SysRq because orderly shutdown can hang on the same
   disk I/O. This is a last-resort availability tradeoff: interrupted metadata
   writes can require further recovery; no crash-safety guarantee is made for
   every LVM operation.

The next boot sees the ESP marker before opening or repairing data storage,
leaves storage failed, and starts bootstrap diagnostics. The guard uses the existing ESP `/reefy/` namespace preserved by firmware
updates. There is no automatic retry or marker expiry. The A/B watchdog leaves this diagnostic session running.
BootNext retries this firmware once without changing BootOrder; an operator
must account for older firmware that does not understand the guard before
subsequent manual reboots or slot changes. Failed ESP persistence or BootNext
verification forbids the forced reboot. If a writer or mount remains stuck,
storage stays activating; console intervention is then required.

Control operations (marker worker, EFI commands, unmount) have 30-second
bounds so an unhealthy device cannot trap the supervisor indefinitely;
journal collection is limited to five seconds. A process blocked in kernel
I/O may survive a signal. Such failures never authorize mounting an unverified
filesystem. The supervisor does not bound initial ESP mounting, cannot run
through a kernel-wide hang, and does not replace the hardware watchdog.

An operator should preserve and inspect the report, stop data-plane consumers,
and establish a safe recovery plan before removing either marker from the
writable ESP and retrying. No automatic reset, formatting or provisioning is
performed. The report contains device-local diagnostics and is not uploaded
to the cloud by this feature.

The QEMU regression uses an accelerated budget with interruptible and
signal-resistant synthetic recovery processes. It verifies real service
failure, MQTT registration/terminal access, provisioning refusal and a real
reboot into the persistent guard. Unit tests cover failed persistence, surviving
descendants, mount cleanup and firmware selection. These tests validate the
backstop mechanism, not the safety of interrupting every possible repair write.

## Thin metadata reconstruction refusal

Automatic thin repair requires a readable current superblock and mapping
roots. The internal LVM repair hook fingerprints a complete ordinary
`thin_dump`, runs upstream `thin_repair` into the separate spare, checks the
output with `thin_check`, and requires an identical native dump before LVM
may swap the metadata LV. Dumps are streamed into SHA-256 rather than saved
on the RAM root. No transaction or geometry override authorizes guessing
historical roots. This still permits space-map repair when current mappings
survive. A changed dump, unreadable mapping root, or lost superblock requires
operator recovery; successful mounting is not a substitute for this check.

When thin repair is refused, boot retains the thick identity LV for control
when available, marks storage failed, blocks reconciliation and provisioning,
and leaves MQTT diagnostics reachable. The A/B watchdog also respects the
runtime storage-failure guard, preventing automatic rollback into an older
repair implementation. It does not confirm a failed firmware slot. This
runtime guard does not prevent an operator from manually choosing an older
slot on a subsequent reboot.

Checksums and matching dumps do not prove application-file correctness or
protect against a device returning a self-consistent stale image. Offline XFS
checks and application-level verification remain separate requirements.
