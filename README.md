# DSH × Qwen-Image-2.1：在模型端给四台 DGX Spark 加一个图像模型

四台 DGX Spark（GB10）本来一起跑 DeepSeek V4.1 Flash（vLLM，TP4）。
这个方案**不动推理服务**，在模型端加一个 OpenAI 兼容网关：同一套 `/v1` 里多出
`qwen-image-2.1` 这个「模型」，任何客户端（DSH、Cursor、OpenAI SDK、curl）选中它就能出图，
请求自动派到四台里最闲的一台。

```
                ┌─ gua1 192.168.2.208:8188  ComfyUI + Qwen-Image-2.1 INT8 ┐
客户端 ── /v1 ──→ studio-gateway :8100 ─┼─ gua2 192.168.2.28:8188        │ 每台同时
 (任何 OpenAI   │  ├ 文本 → :8000 vLLM   ├─ gua3 192.168.2.103:8188       │ 跑 DeepSeek
  兼容客户端)   │  └ 图像 → 四台轮询      └─ gua4 192.168.2.124:8188       ┘ rank
```

## 用法

Base URL 指向网关，模型名选 `qwen-image-2.1`（快，8 步）或 `qwen-image-2.1-base`（20 步，cfg 2.5）：

```bash
curl http://192.168.2.208:8100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-image-2.1","messages":[{"role":"user","content":"a lighthouse at dawn, oil painting"}]}'
```

返回标准 chat.completion，`content` 里是一个可直接打开的图片链接：

```json
{"choices":[{"message":{"role":"assistant","content":"![image](http://192.168.2.208:8100/images/1e33....png)\n\n1328x1328 · seed 327555711 · 17.1s"}}],
 "studio":{"server":"http://192.168.2.208:8188","seconds":17.1}}
```

也支持 OpenAI 的图片接口：

```bash
curl http://192.168.2.208:8100/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-image-2.1","prompt":"a snowy owl","size":"1024x1024"}'
# → {"created":...,"data":[{"url":"http://192.168.2.208:8100/images/....png"}]}
```

文本请求原样转发给 vLLM（含流式、工具调用、`-off/-low/-high/-max` 档位），
所以客户端只配一个 base URL 就能同时用文本和图像。

## 实测

| 场景 | 耗时 |
|---|---|
| 单张 768×768 | 7–12 s |
| 单张 1328×1328 | 16–17 s |
| 4 个并发请求，分摊到 4 台 | 7.6 s 全部返回 |

负载均衡按「在跑的任务数 → ComfyUI 队列深度」原子选节点，并发请求不会挤在同一台。

## 安装

1. **每台**装 ComfyUI + 模型（`scripts/node-setup.sh`，密码作为参数）：

```bash
bash node-setup.sh 22222222
```

脚本做四件事：拉 ComfyUI `3dd559d`（0.36.0）、下 INT8 模型到 `~/studio/models/Qwen-Image-2.1`、
建 venv 装依赖、起容器 `comfy-studio`（`:8188`）。

2. **每台**起内存守卫（`MemAvailable < 4 GB` 时只杀 studio，不碰 DeepSeek）：

```bash
nohup bash guard.sh comfy-studio 4096 &
```

3. **主节点**起网关：

```bash
bash deploy-gateway.sh     # 起容器 studio-gateway，监听 :8100
```

## 关键坑

1. **ComfyUI 必须用 commit `3dd559d`**。`v0.36.0` 这个 tag 是旧树：没有 `TextEncodeQwenImage21`，
   加载 Qwen-Image-2.1 的 VAE 会报 `shape [576] vs [512]`。
2. **pip 加 `--no-deps`**，否则会顺带把 `nvidia-nccl-cu13` 降到 2.29.7 塞进 venv，覆盖系统的 2.30.7。
   加了 `--no-deps` 后要手动补 `trampoline`（torchsde）和 `kornia-rs`（kornia），缺了容器启动即退出。
3. **`--disable-pinned-memory` 必开**：ComfyUI 默认 page-lock 大量 host 内存，在 GB10 上就是抢 DeepSeek 的统一内存。
4. **`--reserve-vram` 按实际 rank 占用给**：本机 DeepSeek 占约 89 GB，所以给 88。
5. **容器 `--restart no` + 守卫**：宁可 studio 挂，也不要 OOM 杀掉推理服务。
6. 模型：INT8-ConvRot 四件套约 23 GB（主模型 6.76G + turbo 6.76G + 文本编码器 8.71G + VAE 0.63G）。

## 在 DSH 里直接用

DSH 的 provider 配置里加一段（`~/.dsh/profiles/<profile>/cordis.patch.yml`），
模型菜单里就会出现 Qwen-Image，选中后像聊天一样出图：

```yaml
- id: llm-pi-ai
  config:
    providers:
      gb10-image:
        displayName: GB10 Qwen-Image
        api: openai-completions
        baseURL: http://192.168.2.208:8100/v1
        apiKeyEnv: DEEPSEEK_API_KEY
        models:
          - id: qwen-image-2.1
            name: Qwen-Image-2.1 · 快速（8 步）
            contextWindow: 8192
            maxTokens: 1024
            input: [text]
          - id: qwen-image-2.1-base
            name: Qwen-Image-2.1 · 精细（20 步）
            contextWindow: 8192
            maxTokens: 1024
            input: [text]
```

重启 DSH 后生效。（本仓库的 `plugin/` 是另一种用法：给 agent 注册 `studio_image` 工具，二选一即可。）

## 可选：DSH 插件

`plugin/` 是一个 DSH Host 插件，注册 `studio_image` 工具，让 agent 直接画图（四台轮询）。
装了它就在对话里说「画一张……」，不用手写请求；不装也不影响模型端用法。

## 目录

```
scripts/studio-gateway.py   模型端网关（OpenAI 兼容，文本转发 + 图像轮询）
scripts/deploy-gateway.sh   起网关容器
scripts/node-setup.sh       单台安装：ComfyUI + 模型 + venv + 容器
scripts/guard.sh            内存守卫
scripts/run-studio.sh       只起 studio 容器
plugin/                     DSH 插件（可选）
```
