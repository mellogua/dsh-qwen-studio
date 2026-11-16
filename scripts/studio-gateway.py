"""studio-gateway — OpenAI-compatible facade that adds Qwen-Image-2.1 models to the DGX Spark API.

Text requests are proxied unchanged to the DeepSeek vLLM instance (127.0.0.1:8000).
Requests for an image model are rendered on the least-busy ComfyUI studio (four Sparks) and
returned as an OpenAI-style response whose content links the generated PNG.
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

UPSTREAM = os.environ.get("STUDIO_UPSTREAM", "http://127.0.0.1:8000")
PUBLIC_BASE = os.environ.get("STUDIO_PUBLIC_BASE", "http://192.168.2.208:8100")
OUT_DIR = Path(os.environ.get("STUDIO_OUT", str(Path.home() / "studio" / "gateway" / "out")))
SERVERS = [
    "http://192.168.2.208:8188",
    "http://192.168.2.28:8188",
    "http://192.168.2.103:8188",
    "http://192.168.2.124:8188",
]
IMAGE_MODELS = {"qwen-image-2.1", "qwen-image-2.1-base", "qwen-image-2.1-turbo"}

TURBO_UNET = "qwen_image_2.1_turbo_int8_convrot.safetensors"
BASE_UNET = "qwen_image_2.1_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0"

OUT_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="studio-gateway", docs_url=None, redoc_url=None)
app.mount("/images", StaticFiles(directory=str(OUT_DIR)), name="images")
_inflight = {s: 0 for s in SERVERS}
_depth = {s: 0 for s in SERVERS}
_select_lock = asyncio.Lock()


async def _poll_depths() -> None:
    """Keep per-studio queue depth fresh without slowing down request admission."""
    while True:
        async with httpx.AsyncClient(timeout=3.0) as client:
            for server in SERVERS:
                try:
                    r = await client.get(f"{server}/queue")
                    q = r.json()
                    _depth[server] = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
                except Exception:
                    _depth[server] = 0
        await asyncio.sleep(2.0)


def build_graph(prompt: str, width: int, height: int, seed: int, quality: str) -> dict:
    turbo = quality != "base"
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": TURBO_UNET if turbo else BASE_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "studio/GW"}},
    }
    if turbo:
        g["11"] = {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["4", 0]}}
        g["12"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": SIGMAS}}
        g["13"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
        g["14"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
        g["8"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["14", 0], "guider": ["11", 0], "sampler": ["13", 0],
            "sigmas": ["12", 0], "latent_image": ["6", 0]}}
    else:
        g["5"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}}
        g["7"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.1}}
        g["8"] = {"class_type": "KSampler", "inputs": {
            "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0],
            "seed": seed, "steps": 20, "cfg": 2.5, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0}}
    return g


async def pick_server() -> str:
    """Reserve the least busy studio atomically so concurrent requests fan out."""
    async with _select_lock:
        server = min(SERVERS, key=lambda s: (_inflight[s], _depth[s]))
        _inflight[server] += 1
        return server


async def render(prompt: str, width: int, height: int, seed: int, quality: str) -> dict:
    server = await pick_server()
    started = time.time()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(f"{server}/prompt", json={"prompt": build_graph(prompt, width, height, seed, quality)})
            if r.status_code >= 400:
                raise RuntimeError(f"studio {server} rejected: {r.status_code} {r.text[:200]}")
            prompt_id = r.json()["prompt_id"]

            images = None
            while time.time() - started < 600:
                await asyncio.sleep(1.0)
                try:
                    h = await client.get(f"{server}/history/{prompt_id}", timeout=10.0)
                    entry = h.json().get(prompt_id)
                except Exception:
                    continue
                if not entry:
                    continue
                if entry.get("status", {}).get("status_str") == "error":
                    raise RuntimeError("studio render failed")
                found = [im for node in entry.get("outputs", {}).values() for im in node.get("images", [])]
                if found:
                    images = found
                    break
            if not images:
                raise RuntimeError("studio timed out")

            img = images[0]
            q = {"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")}
            v = await client.get(f"{server}/view", params=q, timeout=120.0)
            v.raise_for_status()
            name = f"{uuid.uuid4().hex}.png"
            (OUT_DIR / name).write_bytes(v.content)
            return {
                "url": f"{PUBLIC_BASE}/images/{name}",
                "server": server,
                "seed": seed,
                "width": width,
                "height": height,
                "seconds": round(time.time() - started, 1),
            }
        finally:
            _inflight[server] -= 1


def parse_size(value) -> tuple[int, int]:
    if isinstance(value, str) and "x" in value:
        w, h = value.lower().split("x", 1)
        return int(w), int(h)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    return 1328, 1328


def last_user_text(messages) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            if parts:
                return " ".join(parts)
    return ""


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_poll_depths())


@app.get("/healthz")
async def healthz():
    return {"ok": True, "inflight": _inflight, "depth": _depth}


@app.get("/v1/models")
async def models():
    out = {"object": "list", "data": []}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{UPSTREAM}/v1/models")
            out = r.json()
    except Exception:
        pass
    now = int(time.time())
    for name in ("qwen-image-2.1", "qwen-image-2.1-base"):
        out["data"].append({
            "id": name, "object": "model", "created": now, "owned_by": "dsh-studio",
            "root": name, "parent": None, "max_model_len": 0, "permission": [],
        })
    return out


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    body = await request.json()
    prompt = body.get("prompt") or ""
    if not prompt.strip():
        return JSONResponse({"error": {"message": "prompt is required"}}, status_code=400)
    width, height = parse_size(body.get("size"))
    seed = int(body.get("seed") or int.from_bytes(os.urandom(4), "big") // 2)
    quality = "base" if str(body.get("model", "")).endswith("-base") else "turbo"
    result = await render(prompt, width, height, seed, quality)
    return {"created": int(time.time()), "data": [{"url": result["url"], "revised_prompt": prompt}], "studio": result}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = str(body.get("model", ""))
    if model in IMAGE_MODELS:
        prompt = last_user_text(body.get("messages"))
        if not prompt.strip():
            return JSONResponse({"error": {"message": "no user text found"}}, status_code=400)
        width, height = 1328, 1328
        result = await render(prompt, width, height, int.from_bytes(os.urandom(4), "big") // 2,
                              "base" if model.endswith("-base") else "turbo")
        content = (f"![image]({result['url']})\n\n"
                   f"{result['width']}x{result['height']} · seed {result['seed']} · {result['seconds']}s\n\n"
                   f"{result['url']}")
        now = int(time.time())
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "studio": result,
        }

    headers = {k: v for k, v in request.headers.items() if k.lower() in ("authorization", "content-type")}
    raw = await request.body()
    stream = bool(body.get("stream"))
    client = httpx.AsyncClient(timeout=None)
    req = client.build_request("POST", f"{UPSTREAM}/v1/chat/completions", content=raw, headers=headers)

    if not stream:
        resp = await client.send(req)
        await client.aclose()
        return JSONResponse(resp.json(), status_code=resp.status_code)

    resp = await client.send(req, stream=True)

    async def gen():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


@app.get("/images/{name}")
async def image(name: str):
    path = OUT_DIR / name
    if not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("STUDIO_PORT", "8100")), log_level="info")
