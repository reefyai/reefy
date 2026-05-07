<p align="center">
  <img src="images/reefy-logo.png" alt="Reefy" width="120">
  <h1 align="center">Turn your PC into a Reef</h1>
  <p align="center">
    <b>Cloud-like experience on your own hardware. No monthly bills.</b>
  </p>
</p>

<p align="center">
  <img src="images/reefy-dashboard.png" alt="Reefy Dashboard" width="800">
</p>

---

## Why Reefy?

**Why rent a cloud VPS for $10/month when the old PC under your desk can do more - for free?**

A $39 Intel NUC from eBay - 2-core Celeron, 8GB RAM, 128GB SSD - can run dozens of AI agents. The hardware you already own is more powerful than most people realize. You just need the right software to unlock it.

**Why send your private data to someone else's computer?** Your conversations with AI, your camera feeds, your documents - they don't need to live on a server you don't control.

**Reefy gives your own PC a cloud-like experience** - automated updates, encrypted backups, remote access, one-click app installs - without the cloud. This is what drove people to the cloud in the first place: convenience, not capability. Reefy brings that convenience home.

We're not replacing the cloud - we love clouds and Reefy uses them a lot under the hood! We're moving your personal work closer to you.

## Reimagined OS, built bottom-up for the AI era

Reefy OS isn't another shell on top of Ubuntu or Debian with a web UI bolted on. We started from Buildroot and hand-picked every package from the kernel up: 15-second cold boot, Nvidia GPU as a first-class citizen, immutable A/B root with auto-rollback, encryption keyed to a USB dongle, and no package manager on the device so the system never drifts. The app catalog is AI-focused from day one - AI agents (OpenClaw, Hermes), local LLM inference (Ollama, vLLM, SGLang), vision pipelines - not retrofitted onto a general-purpose distro. Legacy distros weren't designed to boot fast, run GPU workloads natively, survive bad updates in remote closets, and keep your data on your side of the wire. Reefy OS was.

## What you can do with Reefy

### Control your PC from anywhere

Secure tunnels to every device. No port forwarding, no VPN, no static IP needed. Built-in web terminal with on-screen control buttons gives you a real terminal experience even from your phone.

<p align="center"><img src="images/feature-mobile-terminal.gif" alt="Reefy web terminal on iPhone with on-screen modifier-key buttons" width="480"></p>

Reefy also gives every running app its own terminal, so you don't have to manually `docker exec` into your agent. All sessions run inside `tmux` - close the browser, switch devices, come back later, and pick up exactly where you left off.

<p align="center"><img src="images/reefy-terminal-hermes.png" alt="Reefy terminal attached to Hermes Agent container" width="800"></p>

### Run private AI agents

Run multiple OpenClaw and Hermes agents. Spin them up for different tasks, fully isolated.

<p align="center"><img src="images/feature-agents.gif" alt="Adding multiple OpenClaw and Hermes agents in the Reefy dashboard" width="480"></p>

### Move to a new PC like it's a new iPhone

Your apps and data are encrypted and backed up to the cloud. Use Adopt & Clone to restore everything and get back to work in minutes.

<p align="center"><img src="images/feature-adopt-clone.gif" alt="Adopt & Clone restoring apps and data on a fresh Reefy device" width="480"></p>

### NVIDIA GPU acceleration, out of the box

Reefy ships with NVIDIA drivers built-in - just start Ollama or any other GPU-aware tool and it works.

<p align="center"><img src="images/feature-ollama-gpu.gif" alt="Ollama running on Reefy with NVIDIA GPU acceleration" width="480"></p>

### Can't brick it

A/B firmware with a hardware watchdog: a bad update auto-rolls back to the previous working version. Critical for devices in closets, attics, and other hard-to-reach places.

<p align="center"><img src="images/feature-ab-updates.gif" alt="Reefy A/B firmware update auto-rolling back after a failed boot" width="480"></p>

### And more from the app catalog

Install apps from the built-in catalog with one click. Full catalog available once you adopt your first device.

| App | Description |
|-----|-------------|
| [OpenClaw](https://github.com/openclaw/openclaw) | Personal AI assistant gateway |
| [Hermes Agent](https://github.com/nousresearch/hermes-agent) | Autonomous AI agent |
| [Ollama](https://ollama.ai) | Run LLMs locally (Nvidia GPU accelerated) |
| [vLLM](https://github.com/vllm-project/vllm) | High-performance LLM inference (Nvidia GPU accelerated) |
| [SGLang](https://github.com/sgl-project/sglang) | High-throughput LLM serving (Nvidia GPU accelerated) |
| [Frigate](https://frigate.video) | Real-time camera NVR with object detection |
| [PhotoPrism](https://photoprism.app) | Self-hosted photo management |
| Dev Ubuntu | Full Ubuntu development environment |
| Dev Fedora | Full Fedora development environment |
| Guppy | Lightweight test app for developers |

Reefy apps are regular Docker containers - bring your own image or wrap any existing one as a Reefy app.

## Key Features

**Zero-touch setup** - Flash a USB, boot any x86 machine, see it on your [dashboard](https://reefy.ai) in 60 seconds.

**15-second boot** - We optimized the Linux kernel and early boot services to eliminate ugly waits. From power button to running apps - blazing fast.

**Can't brick it** - A/B firmware with hardware watchdog. Bad update? Device automatically rolls back to the previous working version. Critical for devices in closets, attics, and remote locations.

**Works offline** - LAN access to all your apps when internet goes down. Devices issue their own SSL certificates that you can import into your browser for a near-internet experience without real internet. Can run fully air-gapped - think internet on the Moon or Mars when people start building habitats there, or security-sensitive environments with intentionally disconnected networks.

**Nvidia GPU support** - Latest Nvidia drivers included out of the box. Any Reefy app can access the GPU - run LLMs, AI inference, video processing, or CUDA workloads without driver hassles.

**Bare metal performance** - Docker containers run directly on x86 hardware. No hypervisor overhead. Every watt and every megabyte goes to your workload - especially important on small, low-power PCs.

**Encrypted & portable** - Data encrypted on disk with keys on the USB dongle. Pull the USB and the device is a paperweight - safe to give away or sell. Plug the USB into a new PC, restore from backup, and you're back in minutes. Have a spare device? You can achieve 99.9% availability (the cloud gold standard - under 9 hours downtime per year) by switching your workloads to the spare in minutes.

**Automated backups** - Your apps and data are backed up automatically. Restore to any device with one click. Replace a broken machine without losing anything. Pin a backup with a tag and use it as a baseline to multiply your agents - configure your OpenClaw or Hermes Agent once, then clone it to new devices without repeating the initial setup.

**One-click app install** - Deploy apps from the catalog or develop your own Reefy apps based on Docker containers. Each app gets its own isolated environment, accessible from anywhere with a secure link.

## Get Started

### 1. Bring a PC
Old laptop, mini-PC, used NUC, or a high-end server with GPUs.

<p align="center"><img src="images/start-hero.jpg" alt="Mini-PC on a desk - any x86_64 box works" width="600"></p>

### 2. Sign in and download your Reefy image
Log in at [reefy.ai](https://reefy.ai) with your Google or GitHub account - one click, no signup forms. Then grab your personalized device image from Settings.

<p align="center"><img src="images/step2-download.jpg" alt="Reefy download dialog showing reefy.raw image" width="600"></p>

### 3. Flash to a USB stick
Use [Balena Etcher](https://etcher.balena.io/) - cross-platform, drag the `reefy.raw` file in, pick your USB, click flash.

<p align="center"><img src="images/step3-balena.jpg" alt="Balena Etcher mid-flash, writing reefy.raw to a USB drive" width="600"></p>

<details><summary>Command-line (Linux/macOS, no GUI)</summary>

`sudo dd if=reefy.raw of=/dev/sdX bs=4M`

</details>

### 4. Boot the PC from USB
Plug in the USB, power on, hit `Esc` or `Del` to open the boot menu. Pick the USB. Reefy boots in ~15 seconds.

<details><summary>Optional: if boot is blocked</summary>

Disable Secure Boot in your BIOS. Common path: *Security → Secure Boot → Disabled*.

</details>

### 5. Adopt the device
Your device shows up automatically in the [Reefy dashboard](https://reefy.ai) once it's online. Click **Adopt**, give it a name. Done.

<p align="center"><img src="images/step5-adopt.gif" alt="Reefy dashboard adopting a new device" width="600"></p>

---

<p align="center">
  <b>Ready to turn your PC into a Reef?</b><br>
  <a href="https://reefy.ai">Get started at reefy.ai →</a>
</p>
