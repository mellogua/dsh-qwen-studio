# 在 DeepSeek API 后面挂一个会自动出图的 Qwen-Image-2.1

四台 DGX Spark（GB10）本来用 vLLM TP4 跑 DeepSeek V4.1 Flash。
这个方案**只改服务器端**：客户端仍然只连 DeepSeek 的地址、用原来的模型名和密钥，
当用户说「画一张……」时，DeepSeek 自己调用 `qwen_image` 工具，图片由四台里最闲的一台 Spark 渲染，
最终回复里直接带图。

客户端不需要装插件、不需要换 base URL、不需要多选一个模型。

```
                           ┌─ gua1 192.168.2.208:8188  ComfyUI + Qwen-Image-2.1 INT8 ┐
client ── 原 DeepSeek API ─┐
  https://…:8443/v1       ├→ auto-proxy :8090 ─┬→ 文本：vLLM 8000（原样转发，含 SSE / tools）
  http://…:8000/v1        │                    └→ 出图：studio-gateway :8100 ─┼─ gua2 :8188
                           └─ （公网隧道 18080）                                ├─ gua3 :8188
                                                                                └─ gua4 :8188
```

- **auto-proxy（:8090）**：OpenAI 兼容的透明层。普通请求逐字节转发给 vLLM（流式、工具调用、错误码、`-off/-low/-high/-max` 全保留）；
  只有最后一条用户消息像「要图」的请求，才注入 `qwen_image` 工具并强制调用一次，拿到图片后把工具结果回灌给模型生成最终回答。
- **studio-gateway（:8100）**：图片网关。维护四台 studio 的在跑任务数 + 队列深度，原子选最闲的一台，返回图片 URL。
- **studio（四台 :8188）**：ComfyUI 0.36 + Qwen-Image-2.1 INT8，和 DeepSeek rank 同机共存，内存守卫兜底。

## 效果

用户：`帮我画一张：雨夜的东京小巷，霓虹倒影，电影感`

回复（自动，无需任何参数）：

```
画好了 🌧️
![image](http://192.168.2.208:8100/images/52c5….png)
画面里希望呈现的几点：…
```

## 部署

### 1. 每台 Spark 装 studio

```bash
bash scripts/node-setup.sh <sudo密码>     # ComfyUI 3dd559d + INT8 模型 + venv + 容器 :8188
nohup bash scripts/guard.sh comfy-studio 4096 &   # MemAvailable < 4GB 时只杀 studio
```

### 2. 主节点起图片网关

```bash
bash scripts/deploy-gateway.sh            # 容器 studio-gateway，:8100
curl -s http://127.0.0.1:8100/healthz
```

### 3. 主节点起自动代理

```bash
bash scripts/deploy-auto.sh               # 容器 studio-auto，:8090
```

### 4. 把入口切到 :8090

公网走 SSH 反向隧道，改一行即可（先备份）：

```bash
sudo sed -i 's#18080:127.0.0.1:8000#18080:127.0.0.1:8090#' /etc/systemd/system/gb10-public-api-tunnel.service
sudo systemctl daemon-reload && sudo systemctl restart gb10-public-api-tunnel
```

局域网想让 `:8000` 也自动出图，把 vLLM 挪到 `:8001` 再把代理放 `:8000` 即可；
本方案公网与 DSH 都走 `:8090`，客户端配置零改动。

### 5. 公网图片路由（可选，但公网用户要看到图就必须加）

Caddy 默认只代理 `/v1/` 并校验 Bearer，浏览器打开图片链接不会带 header，所以要在
`/etc/gb10-public-api/Caddyfile` 的 fallback `handle` 之前加一段免鉴权的只读路由：

```
    @images {
        path /images/*
        method GET
    }
    handle @images {
        reverse_proxy 127.0.0.1:18080 {
            flush_interval -1
            transport http {
                response_header_timeout 60s
                read_timeout 300s
            }
        }
    }
```

`caddy validate` → `systemctl reload gb10-public-api`，只热加载，不动其他站点。
改完 diff 确认只有这一个文件多了 13 行。

代理会按请求的 `Host` 决定返回哪种链接：公网请求给 `https://…:8443/images/…`，局域网请求给
`http://192.168.2.208:8100/images/…`。

> 带宽：1328² PNG 约 2.7–3.0 MB/张。VPS 是 50 Mbps / 400 GB 月流量，正常用绰绰有余。

## 验收记录（2026-11-17）

| 检查 | 结果 |
|---|---|
| 公网 `/v1/models` 无 key / 错 key / 正确 key | 401 / 401 / 200 |
| 公网流式回答 | 10 个 SSE 帧 + `data: [DONE]` |
| 公网出图（`帮我画一张：…`） | 42.2 s，回复内是 `https://64.83.27.15:8443/images/…` |
| 该图片链接（不带 key） | 200 `image/png` 3.0 MB |
| 局域网旁路 `verify-streaming.sh` | 4/4 通过（models / SSE+DONE / 鉴权透传 / 出图） |
| VPS 改动范围 | 只改 `Caddyfile`（+13 行），unit、证书、其他站点均未动 |

## ⚠️ 切换入口前必须做的验证

**模型列表 200 和 `/healthz` 200 不能证明流式可用。** 我们踩过一次：代理返回 `StreamingResponse` 后
提前退出 `AsyncClient` 的上下文，把还在输出的上游连接关掉，客户端收到 `TRANSPORT terminated`。
表现是「模型列表正常、普通请求正常，但 DSH 的流式回答中途断」。

所以代理必须**旁路端口先验真**：

```bash
bash scripts/verify-streaming.sh http://127.0.0.1:8090 <API密钥>
# [2/4] 检查 SSE 帧 + data: [DONE]；[4/4] 检查出图链路
```

只有全部通过，再去改隧道。改完立刻在客户端跑一次流式会话和一次出图。

修复要点（`auto-proxy.py`）：自定义 `StreamingResponse` 子类，在响应体真正结束后才 `aclose()` 上游连接和 client，
不要用 `async with httpx.AsyncClient(...)` 包住 `yield`。

## 关键坑

1. **ComfyUI 必须用 commit `3dd559d`**。GitHub 上 `v0.36.0` 这个 tag 是旧树：没有 `TextEncodeQwenImage21`，
   加载 Qwen-Image-2.1 的 VAE 会报 `shape [576] vs [512]`。
2. **pip 用 `--no-deps`**，否则会把 `nvidia-nccl-cu13` 降到 2.29.7 覆盖系统版本；装完补 `trampoline`、`kornia-rs`，缺了容器启动即退出。
3. **`--disable-pinned-memory` 必开**：ComfyUI 默认 page-lock 大量 host 内存，在 GB10 上是抢 DeepSeek 的统一内存。
4. **`--reserve-vram` 按 rank 实际占用给**：本机 DeepSeek 约 89 GB，所以给 88。
5. **studio 容器 `--restart no` + 守卫**：宁可 studio 挂，也不要 OOM 杀掉推理服务。
6. **`tool_choice` 要强制**：只加 `tools` 时模型常常只回提示词，不调用。代理里对命中「要图」的请求
   直接 `tool_choice={"type":"function","function":{"name":"qwen_image"}}`。
7. 出图 URL 是 `http://192.168.2.208:8100/images/…`（局域网）。公网用户要看到图，需要把 `/images/` 也放进
   Caddy 的路由（当前 Caddy 只代理 `/v1/`）。

## 文件

```
scripts/auto-proxy.py        :8090  透明代理 + 工具调用 + 流式合成
scripts/studio-gateway.py    :8100  四台负载均衡的图片网关
scripts/deploy-auto.sh       起/重启代理容器
scripts/deploy-gateway.sh    起/重启图片网关容器
scripts/node-setup.sh        单台安装 ComfyUI + 模型 + venv + 容器
scripts/guard.sh             内存守卫
scripts/run-studio.sh        只起 studio 容器
scripts/verify-streaming.sh  切换入口前的旁路验证
```
