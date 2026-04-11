# Reefy Compute Collection

Ansible collection for managing Reefy virtual machines with QEMU/SVSM support.

## Features

- **LVM Storage Configuration**: Automatically discovers and configures unpartitioned drives into a logical volume
- **Bridge Networking**: Sets up systemd-networkd bridge for VM network access
- **Docker Configuration**: Configures Docker daemon to use the storage mount
- **QEMU VM Management**: Custom module for managing VMs with:
  - GPU passthrough (NVIDIA and AMD)
  - PCIe device passthrough
  - AMD SEV-SNP confidential computing
  - Tailscale SSH access
  - Optional data disks

## Requirements

- Ansible 2.14+
- Python 3.9+
- Docker (on target hosts)
- `docker` Python library: `pip install docker`

### Collection Dependencies

```yaml
dependencies:
  community.general: ">=7.0.0"
  community.docker: ">=3.0.0"
  ansible.posix: ">=1.5.0"
```

Install dependencies:
```bash
ansible-galaxy collection install community.general community.docker ansible.posix
```

## Installation

### From source (development)

```bash
# Clone the repository
git clone https://github.com/reefyai/reefy.git
cd reefy

# Add to ansible.cfg
[defaults]
collections_path = ./collections
```

### From Galaxy (when published)

```bash
ansible-galaxy collection install reefy.compute
```

## Quick Start

### 1. Configure Inventory

Edit `inventory/hosts.yml`:
```yaml
all:
  children:
    gpu_hosts:
      hosts:
        worker-gpu-01:
          ansible_host: 192.168.1.101
```

### 2. Set Tailscale Key

```bash
export REEFY_VM_TSKEY="tskey-auth-xxxxx"
```

Or add to `inventory/group_vars/all.yml`:
```yaml
reefy_vm_tskey: "{{ lookup('env', 'REEFY_VM_TSKEY') }}"
```

### 3. Run Playbook

```bash
# Start a VM
ansible-playbook reefy.compute.start-vm \
  -i collections/ansible_collections/reefy/compute/inventory/hosts.yml \
  -e reefy_vm_tskey=$REEFY_VM_TSKEY

# Preview changes (dry run)
ansible-playbook reefy.compute.start-vm \
  -i inventory/hosts.yml \
  --check --diff

# Start with custom configuration
ansible-playbook reefy.compute.start-vm \
  -i inventory/hosts.yml \
  -e reefy_vm_name=my-vm \
  -e reefy_vm_vcpu=16 \
  -e reefy_vm_mem=32G \
  -e reefy_vm_attach_gpus=true
```

## Roles

### reefy.compute.vm

Main role that orchestrates all components.

```yaml
- hosts: workers
  roles:
    - role: reefy.compute.vm
      vars:
        reefy_vm_tskey: "{{ tailscale_key }}"
        reefy_vm_vcpu: 8
        reefy_vm_mem: "16G"
        reefy_vm_attach_gpus: true
```

#### Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `reefy_vm_name` | auto-generated | VM hostname |
| `reefy_vm_state` | `present` | VM state: present, absent, started, stopped |
| `reefy_vm_vcpu` | `2` | Number of vCPUs |
| `reefy_vm_mem` | `"4G"` | Memory allocation |
| `reefy_vm_image_size` | `"10G"` | Boot disk size |
| `reefy_vm_tskey` | **required** | Tailscale authentication key |
| `reefy_vm_attach_gpus` | `false` | GPU passthrough: `true`, `auto`, or list of PCI addresses |
| `reefy_vm_confidential_computing` | `false` | Enable AMD SEV-SNP |
| `reefy_vm_data_disk_name` | - | Optional data disk name |
| `reefy_vm_data_disk_size` | - | Data disk size |
| `reefy_vm_runcmd` | `[]` | Custom commands to run on first boot (cloud-init runcmd) |
| `reefy_configure_storage` | `true` | Configure LVM storage |
| `reefy_configure_networking` | `true` | Configure bridge networking |
| `reefy_configure_docker` | `true` | Configure Docker daemon |

### reefy.compute.storage

Configures LVM storage from unpartitioned drives.

```yaml
- hosts: workers
  roles:
    - role: reefy.compute.storage
      vars:
        reefy_storage_mount: /mnt/reefy-data
        reefy_vg_name: reefy-vg
        reefy_lv_name: reefy-lv
```

### reefy.compute.networking

Configures bridge networking for VMs.

```yaml
- hosts: workers
  roles:
    - role: reefy.compute.networking
      vars:
        reefy_bridge_name: br0
```

### reefy.compute.docker

Configures Docker daemon to use storage mount.

```yaml
- hosts: workers
  roles:
    - role: reefy.compute.docker
      vars:
        reefy_docker_data_root: /mnt/reefy-data/docker
```

### reefy.compute.monitoring

Deploys Grafana Alloy with optional IPMI and NVIDIA DCGM exporters.

| Variable | Default | Description |
|----------|---------|-------------|
| `reefy_monitoring_grafana_url` | **required** | Grafana Cloud Prometheus push URL |
| `reefy_monitoring_grafana_username` | **required** | Grafana Cloud username |
| `reefy_monitoring_grafana_password` | **required** | Grafana Cloud API key |
| `reefy_monitoring_enable_ipmi` | `true` | Enable IPMI exporter |
| `reefy_monitoring_enable_nvidia` | `true` | Enable NVIDIA DCGM exporter |
| `reefy_monitoring_scrape_interval` | `"60s"` | Metrics scrape interval |

### reefy.compute.frigate

Deploys Frigate NVR for video surveillance with object detection.

| Variable | Default | Description |
|----------|---------|-------------|
| `reefy_frigate_image` | `ghcr.io/blakeblackshear/frigate:0.16.0-tensorrt` | Container image |
| `reefy_frigate_config_path` | `/mnt/reefy-data/fg/config` | Config directory |
| `reefy_frigate_media_path` | `/mnt/reefy-data/fg/media` | Media storage |
| `reefy_frigate_auth_enabled` | `true` | Enable authentication |

### reefy.compute.gpu_fryer

GPU stress testing using Hugging Face gpu-fryer.

| Variable | Default | Description |
|----------|---------|-------------|
| `reefy_gpu_fryer_duration` | `60` | Test duration in seconds |
| `reefy_gpu_fryer_image` | `ghcr.io/huggingface/gpu-fryer:latest` | Container image |

## Modules

### reefy.compute.qemu_vm

Manages QEMU VMs running in Docker containers.

```yaml
- name: Start a VM
  reefy.compute.qemu_vm:
    name: my-vm
    state: present
    vcpu: 8
    mem: "16G"
    tskey: "{{ tailscale_key }}"
    gpus: auto
    confidential_computing: false
  register: vm

- name: Show VM info
  debug:
    msg: "VM {{ vm.name }} running in container {{ vm.container_short_id }}"
```

#### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `name` | yes | - | VM name |
| `state` | no | `present` | present, absent, started, stopped |
| `vcpu` | no | `2` | Number of vCPUs |
| `mem` | no | `"4G"` | Memory |
| `tskey` | yes* | - | Tailscale key (*required for present/started) |
| `gpus` | no | `false` | GPU passthrough |
| `pcie_devices` | no | `[]` | PCIe devices to pass through |
| `confidential_computing` | no | `false` | Enable AMD SEV-SNP |
| `image_url` | no | Ubuntu Noble | Cloud image URL |
| `image_size` | no | `"10G"` | Boot disk size |
| `data_disk_name` | no | - | Secondary disk name |
| `data_disk_size` | no | - | Secondary disk size |
| `storage_path` | no | `/mnt/reefy-data` | Storage directory |
| `bridge` | no | `br0` | Network bridge |
| `container_image` | no | `reefy/svsm` | QEMU container image |
| `persist_boot_image` | no | `true` | Keep boot disk across restarts and on remove |
| `runcmd` | no | `[]` | Custom commands appended to cloud-init runcmd |

#### Return Values

| Key | Description |
|-----|-------------|
| `name` | VM name |
| `state` | Current state |
| `container_id` | Docker container ID |
| `container_short_id` | Short container ID |
| `gpus_attached` | List of attached GPU PCI addresses |
| `image_path` | Path to VM boot image |

## Playbooks

The collection includes the following playbooks:

### VM Management
- `start-vm.yml` - Start a QEMU VM
- `stop-vm.yml` - Stop a VM
- `remove-vm.yml` - Remove a VM

### Infrastructure Setup
- `install-docker.yml` - Install Docker on VMs
- `install-nvidia.yml` - Install NVIDIA drivers and container toolkit
- `mount-data-disk.yml` - Mount data disk

### Monitoring
```bash
# Start monitoring with Grafana Cloud
ansible-playbook -i host, playbooks/run-monitoring.yml \
  -e reefy_monitoring_grafana_url="https://prometheus-xxx.grafana.net/api/prom/push" \
  -e reefy_monitoring_grafana_username="123456" \
  -e reefy_monitoring_grafana_password="glc_xxx"
```
Includes: Grafana Alloy, IPMI exporter (if /dev/ipmi0 exists), NVIDIA DCGM exporter (if nvidia-smi exists)

### Services

**Frigate NVR:**
```bash
ansible-playbook -i host, playbooks/run-frigate.yml
# With custom config:
ansible-playbook -i host, playbooks/run-frigate.yml \
  -e reefy_frigate_local_config=/path/to/config.yaml
```

**TP-Link Omada Controller:**
```bash
ansible-playbook -i host, playbooks/run-omada.yml
```

**Ollama (LLM inference):**
```bash
ansible-playbook -i host, playbooks/run-ollama.yml
```

**vLLM (high-performance LLM inference):**
```bash
# Open models (no token needed)
ansible-playbook -i host, playbooks/run-vllm.yml \
  -e 'reefy_vllm_args="--model Qwen/Qwen2.5-7B-Instruct"'

# Gated models (HF token required)
ansible-playbook -i host, playbooks/run-vllm.yml \
  -e 'reefy_vllm_args="--model meta-llama/Llama-3.1-8B-Instruct"' \
  -e reefy_vllm_hf_token="hf_xxx"
```

**SGLang (LLM inference):**
```bash
# Open models (no token needed)
ansible-playbook -i host, playbooks/run-sglang.yml \
  -e reefy_sglang_model="Qwen/Qwen2.5-7B-Instruct"

# Gated models (HF token required)
ansible-playbook -i host, playbooks/run-sglang.yml \
  -e reefy_sglang_model="meta-llama/Llama-3.1-8B-Instruct" \
  -e reefy_sglang_hf_token="hf_xxx"
```

**LightRAG:**
```bash
ansible-playbook -i host, playbooks/run-lightrag.yml
```

**RAGFlow:**
```bash
ansible-playbook -i host, playbooks/run-ragflow.yml
```

### Testing

**GPU Stress Test:**
```bash
# Run for 60 seconds (default)
ansible-playbook -i host, playbooks/run-gpu-fryer.yml

# Run for 5 minutes
ansible-playbook -i host, playbooks/run-gpu-fryer.yml -e reefy_gpu_fryer_duration=300
```

### Stopping Services
Add `-e reefy_<service>_state=absent` to stop any service:
```bash
ansible-playbook -i host, playbooks/run-monitoring.yml -e reefy_monitoring_state=absent
ansible-playbook -i host, playbooks/run-frigate.yml -e reefy_frigate_state=absent
```

## Variable Precedence

Variables are loaded in this order (later overrides earlier):

1. Role defaults (`roles/vm/defaults/main.yml`)
2. Group vars (`inventory/group_vars/all.yml`)
3. Group-specific vars (`inventory/group_vars/gpu_hosts.yml`)
4. Host vars (`inventory/host_vars/worker-01.yml`)
5. Playbook vars
6. Extra vars (`-e`)

## Examples

### Start a simple VM

```yaml
- hosts: workers
  tasks:
    - reefy.compute.qemu_vm:
        name: dev-vm
        tskey: "{{ lookup('env', 'REEFY_VM_TSKEY') }}"
```

### Start a GPU-enabled ML VM

```yaml
- hosts: gpu_workers
  tasks:
    - reefy.compute.qemu_vm:
        name: ml-trainer
        vcpu: 64
        mem: "128G"
        tskey: "{{ tailscale_key }}"
        gpus: auto
        data_disk_name: datasets
        data_disk_size: "1T"
```

### Start a confidential computing VM

```yaml
- hosts: secure_workers
  tasks:
    - reefy.compute.qemu_vm:
        name: secure-workload
        vcpu: 8
        mem: "32G"
        tskey: "{{ tailscale_key }}"
        confidential_computing: true
```

## License

MIT

## Author

Reefy Team
