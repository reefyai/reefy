# Verified OS root

Firmware built with embedded dm-verity checks the read-only operating system
against a SHA-256 root hash. Forward error correction (FEC) can recover limited
corruption while verification ensures that recovered bytes match that hash.
The firmware remains a single Unified Kernel Image (UKI), with the existing
A/B EFI system partitions used for updates and recovery.

## What is verified

The UKI's `.initrd` section contains an initramfs with:

- `/rootfs.squashfs.verity`: the compressed SquashFS OS, its verification tree
  and FEC parity.
- `/etc/veritytab`: a standard mapping entry with the root hash and layout/FEC options.
- The tools needed to activate verification before mounting the OS.

Early userspace reads the generated mapping entry. The native setup helper
validates the verity parameters, establishes loop devices for the embedded
image and creates the read-only `/dev/mapper/reefy-root` device. It reads every protected
data block before mounting SquashFS and handing control to the operating system.
OverlayFS uses that verified mount as its lower layer. The initramfs debug path
also activates verification before exposing the lower filesystem.

## Where the trusted root hash comes from

The root hash is **not a kernel command-line parameter** in this implementation.
The image builder writes it into the standard `/etc/veritytab` entry inside
the embedded initramfs. Early userspace reads that entry and invokes
`systemd-veritysetup`, whose libcryptsetup integration parses the native options
and activates the read-only kernel mapping. A `roothash=` command-line argument is not used.

The trust chain is:

```text
UEFI verifies the signed UKI
  -> the UKI authenticates its embedded initramfs and configuration
  -> the native setup helper supplies the configured root hash to dm-verity
  -> dm-verity checks the read-only OS blocks
```

This chain requires authenticated boot, such as Secure Boot with the owner's
enrolled signing key. Assemble the complete UKI before signing it. Without
authentication of the UKI, someone able to replace both the OS payload and its
configured root hash can replace the trust anchor too. Merely storing the hash
on a command line would not remove that requirement.

FEC operates after Linux starts. It cannot repair an image rejected by UEFI
signature verification before the kernel runs.

## Failure and recovery

Correctable corruption may be repaired by FEC. Unverifiable data is refused;
there is no fallback that mounts an unverified OS root.

Activation failures and runtime integrity failures use independent recovery
tools retained from the initramfs. Recovery selects an untried alternate Reefy
EFI partition on the same disk. Failed-slot history survives reboot until
storage, the control service and the verified root pass health confirmation.
If no usable alternate remains, recovery stops instead of switching endlessly
between failed slots.

See [A/B firmware updates](a-b-firmware-updates.md) for the slot lifecycle.

## Boundaries and inspection

Verification covers the immutable SquashFS OS. The temporary writable OverlayFS
upper layer and persistent application data are outside this root verification
mapping. FEC parity for the OS does not provide application-data recovery.

On a verity-enabled boot, the effective mapping can be inspected with:

```sh
sudo dmsetup table reefy-root
sudo dmsetup status --noflush reefy-root
findmnt /run/reefy-verity/lower
```

The mapping table includes the configured root hash. Verity status `V` indicates
that the target has not reported uncorrected corruption; `C` indicates corruption.
The retained lower mount is read-only and backed by `/dev/mapper/reefy-root`.

A successful FEC read does not always clear the kernel's corruption history.
In particular, repairing top-level hash metadata can leave status `C` after
recursive verification attempts. Reefy refuses handoff or confirmation of such
a mapping and attempts recovery to a healthy slot. Lower-level hash/data FEC
repair that retains status `V` can continue normally.

Health confirmation also requires the independent integrity monitor to be active,
including a fresh check after waiting for the other critical services.
