# Build Reefy Linux Image

## Prerequisites

Ubuntu 24.04 is the recommended build environment.

Install the required packages:

```bash
sudo apt-get install -y build-essential sed make binutils diffutils gcc g++ \
  bash patch gzip bzip2 perl tar cpio unzip rsync file bc findutils gawk \
  wget python3 git libncurses-dev libssl-dev systemd-boot-efi qemu-utils zip
```

## Build

Clone the repository and initialize submodules:

```bash
git clone https://github.com/reefyai/reefy.git
cd reefy
git submodule init
git submodule update
```

Start the build:

```bash
cd buildroot
make BR2_EXTERNAL=.. reefy_defconfig
make -j $(nproc)
```

## Build Output

After a successful build, the following files are generated in `output/images/`:

| File | Description |
|------|-------------|
| `reefy.efi` | UEFI bootable Reefy image in Unified Kernel Image (UKI) format. Integrates the Linux kernel, kernel arguments (cmdline), and initramfs into a single image. |
| `reefy.raw` | Disk image ready to be written directly to a USB flash drive. Features a GPT partition table and a bootable VFAT partition containing `reefy.efi`. |

## Quick Test

Test the build locally using QEMU:

```bash
./scripts/reefy-local-boot.sh
```

This script automatically detects OVMF BIOS and boots the `reefy.raw` image in QEMU with appropriate settings.

Additional options:
- `-disk <path>` - Boot a custom disk image
- `-snapshot` - Run in read-only mode (changes not saved)
- `-net bridge` - Use bridge networking (requires virbr0)
- `-net tap` - Use TAP networking
- `-help` - Show all available options

Example:
```bash
# Boot with read-only snapshot mode
./scripts/reefy-local-boot.sh -snapshot

# Boot custom image with bridge networking
./scripts/reefy-local-boot.sh -disk /path/to/custom.raw -net bridge
```
