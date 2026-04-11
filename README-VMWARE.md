# Running Reefy Linux as a VMware Guest

## Prerequisites
- VMware Workstation installed on your system.
- A valid [Tailscale.com](https://tailscale.com) key.

## Steps to Set Up

1. **Download the Disk Image**
   - Navigate to the [Releases](https://github.com/reefyai/reefy/releases) section of this repository.
   - Download the `reefy.vhd` disk image.

2. **Create a VMware Virtual Machine**
   - Open VMware and create a new virtual machine with the following settings:
     1. **Guest Operating System**: Select `Other 64-bit`
![Select "Other 64-bit"](images/vmware-reefy-settings-os.png)
     3. **Firmware Type**: Choose `UEFI`.
![Choose "UEFI"](images/vmware-reefy-settings-uefi.png)

3. **Prepare the Tailscale Key**
   - Create a folder named `reefy` on your host system.
   - Inside the `reefy` folder, create a text file named `reefy-tskey.txt`.
   - Paste your Tailscale key into the file.
   - Save the file in **ANSI** format (not UTF).
![Save the file in ANSI format (not UTF)](images/vmware-reefy-text-encoding.png)

4. **Attach the Folder to the VMware VM**
   - Attach the `reefy` folder as a shared folder in the VMware VM settings.
![Attach the "reefy" folder as a shared folder](images/vmware-reefy-settings-share.png)

5. **Start the Virtual Machine**
   - Power on the VMware VM.
![Power on the VMware VM](images/vmware-reefy-boot.png)

   - Once booted, the VM should automatically appear in your Tailscale device list.
   - You can SSH into the VM using OAuth, such as Google Auth.

## What Can I Do with Reefy Linux?
For more details on usage, refer to the following sections in the main [Reefy Linux README](https://github.com/reefyai/reefy):

- **Running the "Hello World" Example on Reefy Linux**
- **How to Start Customer Workloads on Reefy Linux**
