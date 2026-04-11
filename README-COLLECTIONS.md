# Reefy Compute Ansible Collection

The `reefy.compute` collection provides modules and roles for managing QEMU virtual machines with GPU passthrough, Tailscale networking, and optional AMD SEV-SNP confidential computing support.

## Prerequisites

- Docker installed and running on target host
- Python docker library: `pip install docker`
- Tailscale authentication key (get from https://login.tailscale.com/admin/settings/keys)

## Quick Start

Create a VM with all GPUs attached, using maximum available resources:

```bash
ansible-playbook -i myhost, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=my-vm \
  -e reefy_vm_vcpu=max \
  -e reefy_vm_mem=max
```

Note: The trailing comma after `myhost,` is required for single-host inventory.

## Examples

### Create a Basic VM

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=dev-vm
```

### Create a VM with Custom Resources

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=dev-vm \
  -e reefy_vm_vcpu=8 \
  -e reefy_vm_mem=32G \
  -e reefy_vm_image_size=100G
```

### Create a VM with Maximum Resources

Automatically uses all available CPUs minus 2 and all available memory minus 2GB:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=max-vm \
  -e reefy_vm_vcpu=max \
  -e reefy_vm_mem=max
```

### Create a VM Without GPUs

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=no-gpu-vm \
  -e reefy_vm_attach_gpus=false
```

### Create a VM with Specific GPUs

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=gpu-vm \
  -e '{"reefy_vm_attach_gpus": ["0000:01:00.0", "0000:41:00.0"]}'
```

### Create a VM with Data Disk

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=data-vm \
  -e reefy_vm_data_disk_name=my-data \
  -e reefy_vm_data_disk_size=500G
```

### Create a VM with Root Password (for Console Access)

Useful when Tailscale is unavailable and you need console access:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=console-vm \
  -e reefy_vm_root_password=mysecretpassword
```

Access console via: `docker attach <vm-name>`

### Create a VM with Custom Boot Commands

Run custom commands on first boot via cloud-init (e.g., kernel tuning, package installs):

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=tuned-vm \
  -e '{"reefy_vm_runcmd": ["sysctl -w net.ipv4.tcp_window_scaling=0", "apt-get update && apt-get install -y htop"]}'
```

### Create a VM with Custom Tailscale Tags

Tags must be pre-authorized in your Tailscale ACL policy:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=tagged-vm \
  -e reefy_vm_tailscale_tags=tag:reefy,tag:dev
```

### Create a VM with Confidential Computing (AMD SEV-SNP)

Requires AMD EPYC processor with SEV-SNP support:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=secure-vm \
  -e reefy_vm_confidential_computing=true
```

### Stop a VM

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/stop-vm.yml \
  -e reefy_vm_name=my-vm
```

### Remove a VM (Keep Boot Disk)

By default, the boot disk is preserved for later reuse:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/stop-vm.yml \
  -e reefy_vm_name=my-vm \
  -e reefy_vm_remove=true
```

### Remove a VM and Delete Boot Disk

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/stop-vm.yml \
  -e reefy_vm_name=my-vm \
  -e reefy_vm_remove=true \
  -e reefy_vm_persist_boot_image=false
```

### Skip Host Configuration

If storage, networking, and Docker are already configured:

```bash
ansible-playbook -i gpu-server, collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=my-vm \
  -e reefy_configure_storage=false \
  -e reefy_configure_networking=false \
  -e reefy_configure_docker=false
```

### Multiple Hosts

```bash
ansible-playbook -i "host1,host2,host3," collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_vcpu=max \
  -e reefy_vm_mem=max
```

Note: VM names will be auto-generated (e.g., `reefy-vm-quickly-happy-dolphin`).

## Using the Module Directly

For more control, use the `reefy.compute.qemu_vm` module in your own playbooks:

```yaml
- name: Manage VMs
  hosts: all
  tasks:
    - name: Create VM with all options
      reefy.compute.qemu_vm:
        name: my-custom-vm
        state: present
        vcpu: 16
        mem: 64G
        image_size: 200G
        tskey: "{{ tailscale_key }}"
        gpus: true
        confidential_computing: false
        data_disk_name: my-data
        data_disk_size: 1T
        persist_boot_image: true
        root_password: "{{ root_pass }}"
        tailscale_tags: "tag:reefy,tag:prod"
```

## Configuration Reference

### VM Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `reefy_vm_name` | auto-generated | VM name (also used as Tailscale hostname) |
| `reefy_vm_state` | `present` | `present`, `absent`, `started`, `stopped` |
| `reefy_vm_vcpu` | `2` | vCPUs or `max` for auto-calculation |
| `reefy_vm_mem` | `4G` | Memory (e.g., `4G`, `64G`) or `max` |
| `reefy_vm_image_size` | `10G` | Boot disk size |
| `reefy_vm_image_url` | Ubuntu Noble | Cloud image URL |
| `reefy_vm_tskey` | **required** | Tailscale auth key |
| `reefy_vm_tailscale_tags` | `tag:reefy` | Tailscale tags to advertise |
| `reefy_vm_attach_gpus` | `true` | `true`/`false` or list of PCI addresses |
| `reefy_vm_attach_pcie_devices` | `[]` | Additional PCIe devices to passthrough |
| `reefy_vm_confidential_computing` | `false` | Enable AMD SEV-SNP |
| `reefy_vm_data_disk_name` | - | Optional data disk name |
| `reefy_vm_data_disk_size` | - | Data disk size (required if name set) |
| `reefy_vm_persist_boot_image` | `true` | Keep boot disk across restarts |
| `reefy_vm_root_password` | - | Optional root password for console |
| `reefy_vm_runcmd` | `[]` | Custom commands to run on first boot (cloud-init runcmd) |

### Host Configuration Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `reefy_configure_storage` | `true` | Configure LVM storage |
| `reefy_configure_networking` | `true` | Configure bridge networking |
| `reefy_configure_docker` | `true` | Configure Docker daemon |
| `reefy_storage_mount` | `/mnt/reefy-data` | Storage mount point |
| `reefy_bridge_name` | `br0` | Network bridge name |
| `reefy_docker_image` | `reefy/svsm` | Container image for QEMU |

## Advanced: Using Inventory Files

For managing multiple hosts or complex configurations, use inventory files.

### Basic Inventory File

Create `inventory.yml`:

```yaml
all:
  hosts:
    gpu-server-1:
      ansible_host: 192.168.1.10
    gpu-server-2:
      ansible_host: 192.168.1.11
  vars:
    reefy_vm_tskey: "tskey-auth-xxxxx"
    reefy_vm_vcpu: max
    reefy_vm_mem: max
```

Run:

```bash
ansible-playbook -i inventory.yml collections/ansible_collections/reefy/compute/playbooks/start-vm.yml
```

### Inventory with Host-Specific Settings

```yaml
all:
  hosts:
    small-server:
      ansible_host: 192.168.1.10
      reefy_vm_vcpu: 4
      reefy_vm_mem: 16G
      reefy_vm_attach_gpus: false

    large-gpu-server:
      ansible_host: 192.168.1.11
      reefy_vm_vcpu: max
      reefy_vm_mem: max
      reefy_vm_attach_gpus: true
      reefy_vm_confidential_computing: true

  vars:
    reefy_vm_tskey: "tskey-auth-xxxxx"
    reefy_vm_image_size: 100G
```

### Inventory with Groups

```yaml
all:
  children:
    dev_servers:
      hosts:
        dev-1:
          ansible_host: 192.168.1.10
        dev-2:
          ansible_host: 192.168.1.11
      vars:
        reefy_vm_tailscale_tags: "tag:reefy,tag:dev"
        reefy_vm_vcpu: 4
        reefy_vm_mem: 16G

    prod_servers:
      hosts:
        prod-1:
          ansible_host: 192.168.1.20
        prod-2:
          ansible_host: 192.168.1.21
      vars:
        reefy_vm_tailscale_tags: "tag:reefy,tag:prod"
        reefy_vm_vcpu: max
        reefy_vm_mem: max
        reefy_vm_confidential_computing: true

  vars:
    reefy_vm_tskey: "tskey-auth-xxxxx"
```

Run on specific group:

```bash
ansible-playbook -i inventory.yml collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e target_hosts=prod_servers
```

### Using group_vars and host_vars Directories

For large deployments, organize variables in directories:

```
inventory/
├── hosts.yml
├── group_vars/
│   ├── all.yml
│   ├── dev_servers.yml
│   └── prod_servers.yml
└── host_vars/
    ├── dev-1.yml
    └── prod-1.yml
```

`inventory/group_vars/all.yml`:
```yaml
reefy_vm_tskey: "tskey-auth-xxxxx"
reefy_vm_image_size: 50G
```

`inventory/group_vars/prod_servers.yml`:
```yaml
reefy_vm_vcpu: max
reefy_vm_mem: max
reefy_vm_confidential_computing: true
reefy_vm_tailscale_tags: "tag:reefy,tag:prod"
```

`inventory/host_vars/prod-1.yml`:
```yaml
reefy_vm_name: prod-primary
reefy_vm_data_disk_name: prod-data
reefy_vm_data_disk_size: 2T
```

Run:

```bash
ansible-playbook -i inventory/ collections/ansible_collections/reefy/compute/playbooks/start-vm.yml
```

## Debugging

Use `-v` flags to increase verbosity for troubleshooting:

| Flag | Level | Description |
|------|-------|-------------|
| `-v` | 1 | Show task results |
| `-vv` | 2 | Show task input parameters |
| `-vvv` | 3 | Show connection details and QEMU command |
| `-vvvv` | 4 | Show plugin internals, connection scripts |

Example:

```bash
ansible-playbook -i gpu-server, -vvv collections/ansible_collections/reefy/compute/playbooks/start-vm.yml \
  -e reefy_vm_tskey=tskey-auth-xxxxx \
  -e reefy_vm_name=debug-vm
```

At `-vvv` level, the `qemu_vm` module will output the full QEMU command being executed.

## Connecting to VMs

Once a VM is running, connect via Tailscale SSH:

```bash
ssh <vm-name>
```

For console access (if root_password was set):

```bash
docker attach <vm-name>
```

Detach from console with `Ctrl+P Ctrl+Q`.
