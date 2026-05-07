# Reefy Linux Customization Using `reefy-cmds.sh`

## Overview

The `reefy-cmds.sh` file introduces a powerful way to customize Reefy Linux instances during boot. By placing a custom shell script named `reefy-cmds.sh` on a USB flash drive or another supported configuration source, you can define commands and behaviors executed under the busybox shell during the boot process.

This feature is ideal for automating tasks, configuring system settings, or running services at startup.

## How It Works

1. During the boot process, Reefy Linux scans for the `reefy-cmds.sh` file on supported sources (e.g., USB flash drives).
2. If found, the script is executed under the busybox shell.
3. Users can define their custom commands within this script to tailor the instance’s behavior.

## Example Script

Below is an example of a simple `reefy-cmds.sh` script:

```bash
#!/bin/sh

# Get the script name
SCRIPT_NAME="$(basename "$0")"

# Function to print messages with script name prefix
log_message() {
    echo "[$SCRIPT_NAME] $1" > /dev/kmsg
}

# Print welcome message
log_message "Welcome to the system information script!"

# Start a Docker container with Alpine and echo Hello, World!
log_message "Starting a Docker container with 'alpine' to echo Hello, World!:"
docker run alpine echo "Hello, World!" | while read -r line; do
    log_message "$line"
done

```


## Notes on `reefy-tskey.txt`

The existing functionality for processing the `reefy-tskey.txt` file remains unchanged. This means that:

- The `reefy-tskey.txt` file is still processed as part of the boot sequence.
- Users who prefer to omit the `reefy-tskey.txt` file can include a full custom Tailscale `up` command with their desired arguments directly in the `reefy-cmds.sh` file.

## Usage Instructions

1. Create a `reefy-cmds.sh` file using the example above or your custom commands.
2. Place the script on a USB flash drive or another supported configuration source.
3. Boot the Reefy Linux instance with the USB drive connected.
4. Verify the output in the system logs or the console to ensure the script executed as expected.
