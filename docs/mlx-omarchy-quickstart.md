# Serve MLX models on Omarchy M (about 10 minutes, most of it downloading)

For one machine: an Apple Silicon Mac (M1/M2 today) running **Omarchy M** — Omarchy running as
Linux on the Mac, not macOS. If you're on macOS, this isn't it — use
[`grid engine install llama.cpp`](cli.md#engine-setup) instead, same as any other Mac.

Two engines can serve a model on that machine. **llama.cpp**, via Vulkan, is the default and
needs nothing extra — `grid engine install llama.cpp` already picks it up (see
[docs/cli.md](cli.md#engine-setup)). **mlx-omarchy**, covered here, is a second engine for two
reasons: it runs models in MLX format (`mlx-community/*` on Hugging Face, not `.gguf`), and it's
the project [Joshua Warren](https://github.com/joshuaswarren/mlx-omarchy) is building toward the
Neural Engine on, alongside the GPU. Today, on the GPU alone, it is not faster than llama.cpp's
Vulkan build — mlx-omarchy's own numbers put it at 69–71% of native Metal decode on an M1, with
llama.cpp Vulkan as the parity bar it's closing on. Pick it when you want an MLX-format model, or
you're testing where this is headed.

## 0. Confirm the machine (10 seconds, nothing written)

```bash
cat /proc/device-tree/compatible | tr '\0' ' '   # must contain "apple,"
ls /usr/share/vulkan/icd.d/                      # must list asahi_icd.aarch64.json
```

No `apple,` → this isn't an Apple Silicon Mac (a VM's virtual GPU doesn't count — see
[Troubleshooting](#troubleshooting)). No `asahi_icd.aarch64.json` → install the driver first:

```bash
sudo pacman -S vulkan-asahi vulkan-icd-loader
```

`grid engine install mlx-omarchy` checks both of these itself and refuses with the same guidance
if either is missing — this step is just so you're not surprised.

## 1. Install

```bash
grid engine install mlx-omarchy
```

This installs **upstream's latest GitHub release** — unlike llama.cpp, whose build Grid pins,
mlx-omarchy tracks whatever its author shipped most recently, verified against that release's own
`SHA256SUMS`. `--version v0.4.2` pins one release if you need a machine to stay put. It lives in
its own venv (`~/.grid/engines/mlx-omarchy`, Python 3.14) — nothing is installed into a system
Python, and it never touches Omarchy's own package set or launcher menu.

Expect output ending in a device name:

```
✓ mlx-omarchy v0.4.2 installed — it uses this Mac's GPU (Apple M1 Pro).
```

If that line names something other than your chip (or says `llvmpipe`), the Vulkan call landed on
software rendering, not the Apple GPU — see [Troubleshooting](#troubleshooting).

## 2. Pick a model

```bash
grid catalog
```

On this machine, alongside the usual GGUF list, you'll see an MLX section:

```
This Mac can also serve MLX models with the mlx-omarchy engine — nothing to pull,
the engine downloads the repo on first join:
  mlx-community/Qwen2.5-7B-Instruct-4bit  (min 16 GB unified memory)
  mlx-community/Qwen2.5-14B-Instruct-4bit  (min 24 GB unified memory)
  mlx-community/Qwen2.5-32B-Instruct-4bit  (min 36 GB unified memory)
```

There's no `grid pull` step for these — unlike a GGUF, the repo is fetched by the engine itself
the first time you join with it, straight into `~/.cache/huggingface`. Any other `mlx-community/*`
repo on Hugging Face works too; the list above is just a sized starting point for this machine's
memory.

## 3. Join

```bash
grid join <grid> --serve mlx-community/Qwen2.5-7B-Instruct-4bit \
    --engine mlx-omarchy --advertise-as qwen7b --name my-m1
```

`<grid>` is Omagrid, or any grid you run — same as every other join. `--engine` is what picks
mlx-omarchy over the llama.cpp default; without it, `--serve` always means llama.cpp. First join
downloads the model (several GB) before it reports ready, so the first run can take a while —
that's the download, not a hang.

## 4. Use it

```bash
grid models                                    # qwen7b should be listed
grid chat -m qwen7b "write a haiku about local GPUs"
grid stats --verbose                           # named by chip ("Apple M1 Pro"), not "aarch64"
```

Leave the same way as any engine:

```bash
grid leave --engine qwen7b
```

## Troubleshooting

**"mlx-omarchy runs only on an Apple Silicon Mac booted into Linux (Omarchy M)."**
Either this isn't a Mac's chip, or it's a Mac running Omarchy **inside a VM** — Try Omarchy, UTM,
Parallels. A VM's GPU (`virtio-gpu`) is never the real one; there is no way around this short of
booting Linux on the bare metal. See `test_omarchy_m_e2e.sh` at the repo root for the one
exception: a `GRID_MLX_OMARCHY_DEV=1` escape hatch that lets the CLI's own code run inside such a
VM for testing — it never proves the GPU path, and it prints a warning every time it's used.

**"…but no Vulkan driver was found, so it runs on the CPU"** (from `grid engine install llama.cpp`)
or **"needs the Apple GPU's Vulkan driver"** (from mlx-omarchy) — `sudo pacman -S vulkan-asahi
vulkan-icd-loader`, then re-run the install.

**The smoke test names `llvmpipe`.** Same root cause as above — Mesa's software rasterizer
answered instead of Honeykrisp. `vulkaninfo --summary` should list Honeykrisp as the driver; if it
lists `llvmpipe` too, the ICD directory has more than one manifest and something is picking the
wrong one.

**The first `grid chat` after joining takes a long time.** Expected on a first join: the model
downloads and loads in the background the moment the engine starts, and `grid join` doesn't
report ready until a real generation succeeds — which is the point, since serving a request before
the model has loaded would just fail slower, later, on someone else's turn through the relay.

## What this hasn't been run against yet

This engine's install and detection logic is unit-tested (`tests/test_mlx_omarchy_engine.py`,
`tests/test_apple_linux.py`) and its exact commands — `mlx_lm server --model … --host … --port …`
and the `/v1/chat/completions` readiness probe — are checked against mlx-lm's own source at the
release this pins. What no test here can prove is Honeykrisp itself under real load: nothing but a
real M1/M2 running Omarchy M can. If you hit something this doc doesn't cover, a
`grid engine install mlx-omarchy` / `grid join` log plus `vulkaninfo --summary` and `uname -a` is
what's most useful in a report — `test_omarchy_m_e2e.sh receipt` collects exactly that into one
file.
