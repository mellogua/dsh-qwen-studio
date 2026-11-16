# Qwen Image Studio

Adds one tool, `studio_image`, that renders with Qwen-Image-2.1 (INT8) on the four DGX Sparks — a
ComfyUI 0.36 studio on each node at `:8188`, running next to the DeepSeek V4.1 Flash rank — and
writes the PNG into the current workspace under `studio/`.

## Usage

Ask for an image; the agent calls `studio_image` with a prompt, optional size and seed, and gets the
saved path back.

- Size: default 1328×1328, multiples of 16. 1024×1024 is faster.
- `quality: turbo` (default) uses the 8-step turbo checkpoint; `quality: base` uses the full model
  with 20 steps at cfg 2.5 (slower, different look).
- Seeds are returned so a result can be reproduced.

## Nodes

The tool round-robins across `http://192.168.2.208:8188` (gua1), `192.168.2.28:8188` (gua2),
`192.168.2.103:8188` (gua3) and `192.168.2.124:8188` (gua4); a node that refuses the job is skipped
for that call. Each result names the node that rendered it.

## Limits

- One render per Spark: the studio shares the 128 GB unified memory pool with the DeepSeek rank.
  A guard kills the studio if `MemAvailable` drops under 4 GB; containers start with
  `--disable-pinned-memory --fp16-intermediates --reserve-vram 88`.
- Turbo renders take ~15-60 s warm; the first render after start loads ~16 GB of weights.
- If a container is down, `docker start comfy-studio` on that node brings it back (then wait for
  `:8188/system_stats`).
