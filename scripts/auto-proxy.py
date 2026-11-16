"""auto-proxy — keeps the DeepSeek API surface unchanged while letting the model call Qwen-Image.

Clients keep pointing at the same base URL, model id and key. Requests are proxied verbatim to the
vLLM instance; only chat requests whose last user message looks like an image request get a
`qwen_image` tool injected, so the model itself decides to call it. The render runs through the
studio gateway (four Sparks, load balanced) and the final answer is streamed back in OpenAI format.
"""

import asyncio
import json
import os
import re
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

UPSTREAM = os.environ.get("AUTO_UPSTREAM", "http://127.0.0.1:8000")
STUDIO = os.environ.get("AUTO_STUDIO", "http://127.0.0.1:8100")
LAN_BASE = os.environ.get("AUTO_LAN_BASE", "http://192.168.2.208:8100")
PUBLIC_BASE = os.environ.get("AUTO_PUBLIC_BASE", "https://64.83.27.15:8443")

IMAGE_HINT = re.compile(
    r"(画|绘|生成|渲染)[^，。！？\n]{0,10}(图|照片|海报|插画|头像|壁纸|画面)"
    r"|(画|绘)(一|个|张|幅|副)"
    r"|(生成|做|出|来)(一|个|张|幅)(图|照片|海报|插画|头像|壁纸)?"
    r"|(draw|paint|render|generate|create|make).{0,24}(image|picture|photo|illustration|poster|art|wallpaper|logo)"
    r"|(image|picture|photo).{0,12}(of|with|showing)"
    r"|qwen-image",
    re.IGNORECASE,
)

TOOL = {
    "type": "function",
    "function": {
        "name": "qwen_image",
        "description": "生成图片。当用户要求画图、生成图片/照片/海报/插画时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "英文或中文的画面描述，尽量具体"},
                "size": {"type": "string", "enum": ["1024x1024", "1328x1328", "768x768"]},
            },
            "required": ["prompt"],
        },
    },
}

SYSTEM_HINT = (
    "你可以调用 qwen_image 工具生成图片。当用户想要图片时，直接调用它，"
    "然后在最终回复里用 Markdown 图片语法展示：![image](图片URL)。不要编造 URL。"
)

app = FastAPI(title="auto-proxy", docs_url=None, redoc_url=None)


class UpstreamResponse(StreamingResponse):
    """Keep upstream resources open until the ASGI response has finished."""

    def __init__(self, upstream: httpx.Response, client: httpx.AsyncClient):
        self.upstream = upstream
        self.client = client
        connection_tokens = {
            value.strip().lower()
            for value in upstream.headers.get("connection", "").split(",")
        }
        excluded = connection_tokens | {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailer", "transfer-encoding", "upgrade",
        }
        headers = {key: value for key, value in upstream.headers.items()
                   if key.lower() not in excluded}
        if upstream.headers.get("content-type", "").startswith("text/event-stream"):
            headers.setdefault("cache-control", "no-cache")
            headers.setdefault("x-accel-buffering", "no")
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code,
                         headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.upstream.aclose()
            finally:
                await self.client.aclose()


async def forward(request: Request, path: str, raw: bytes):
    headers = {key: value for key, value in request.headers.items()
               if key.lower() in ("authorization", "content-type", "accept")}
    # Ownership moves to UpstreamResponse; leaving an AsyncClient context here
    # would close the socket before StreamingResponse starts consuming it.
    client = httpx.AsyncClient(timeout=None)
    try:
        req = client.build_request(request.method, f"{UPSTREAM}/{path}",
                                   content=raw or None, headers=headers,
                                   params=request.query_params)
        upstream = await client.send(req, stream=True)
        return UpstreamResponse(upstream, client)
    except BaseException:
        await client.aclose()
        raise


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


def wants_image(body: dict) -> bool:
    model = str(body.get("model", ""))
    if model.startswith("qwen-image"):
        return True
    return bool(IMAGE_HINT.search(last_user_text(body.get("messages"))))


async def render(args: dict) -> dict:
    prompt = str(args.get("prompt") or "").strip()
    size = str(args.get("size") or "1328x1328")
    if not prompt:
        return {"error": "empty prompt"}
    async with httpx.AsyncClient(timeout=600.0) as client:
        r = await client.post(f"{STUDIO}/v1/images/generations",
                              json={"prompt": prompt, "size": size})
        if r.status_code >= 400:
            return {"error": f"studio {r.status_code}: {r.text[:200]}"}
        data = r.json()
        studio = data.get("studio", {})
        return {
            "url": data["data"][0]["url"],
            "width": studio.get("width"),
            "height": studio.get("height"),
            "seed": studio.get("seed"),
            "seconds": studio.get("seconds"),
        }


def with_hint(messages: list) -> list:
    msgs = list(messages)
    for i, m in enumerate(msgs):
        if m.get("role") == "system":
            msgs[i] = {**m, "content": str(m.get("content", "")) + "\n\n" + SYSTEM_HINT}
            return msgs
    return [{"role": "system", "content": SYSTEM_HINT}] + msgs


def with_tool(body: dict) -> list:
    tools = list(body.get("tools") or [])
    if not any(t.get("function", {}).get("name") == "qwen_image" for t in tools):
        tools.append(TOOL)
    return tools


async def call_model(client: httpx.AsyncClient, payload: dict, headers: dict) -> dict:
    r = await client.post(f"{UPSTREAM}/v1/chat/completions", json=payload, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"upstream {r.status_code}: {r.text[:300]}")
    return r.json()


def assistant_message(raw: dict) -> dict:
    msg = raw["choices"][0]["message"]
    clean = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        clean["tool_calls"] = msg["tool_calls"]
    if msg.get("reasoning_content"):
        clean["reasoning_content"] = msg["reasoning_content"]
    return clean


def sse(body: dict, model: str, content: str) -> bytes:
    cid = body.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = body.get("created") or int(time.time())

    def chunk(delta: dict, finish=None) -> str:
        payload = {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    out = chunk({"role": "assistant", "content": ""})
    for i in range(0, len(content), 24):
        out += chunk({"content": content[i:i + 24]})
    out += chunk({}, "stop") + "data: [DONE]\n\n"
    return out.encode()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    raw = await request.body()
    body = json.loads(raw)
    headers = {"content-type": "application/json"}
    if request.headers.get("authorization"):
        headers["authorization"] = request.headers["authorization"]

    if not wants_image(body):
        return await forward(request, "v1/chat/completions", raw)

    model = str(body.get("model", ""))
    keep = {k: body[k] for k in ("temperature", "top_p", "max_tokens", "thinking", "reasoning_effort", "chat_template_kwargs") if k in body}
    started = time.time()
    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            first = await call_model(client, {
                "model": model, "messages": with_hint(body["messages"]), "tools": with_tool(body),
                "tool_choice": {"type": "function", "function": {"name": "qwen_image"}},
                "stream": False, **keep,
            }, headers)
            msg = assistant_message(first)
            calls = [c for c in (msg.get("tool_calls") or []) if c.get("function", {}).get("name") == "qwen_image"]
            if not calls:
                content = msg.get("content") or ""
            else:
                results = []
                for call in calls:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if not str(args.get("prompt") or "").strip():
                        args["prompt"] = last_user_text(body["messages"])
                    res = await render(args)
                    host = request.headers.get("host", "")
                    if host and not host.startswith("192.168.") and res.get("url"):
                        res["url"] = res["url"].replace(LAN_BASE, PUBLIC_BASE)
                    results.append((call.get("id", "call_0"), res))
                follow = with_hint(body["messages"]) + [msg] + [
                    {"role": "tool", "tool_call_id": cid, "content": json.dumps(res, ensure_ascii=False)}
                    for cid, res in results
                ]
                second = await call_model(client, {"model": model, "messages": follow, "stream": False, **keep}, headers)
                content = second["choices"][0]["message"].get("content") or ""
                for _, res in results:
                    if res.get("url") and res["url"] not in content:
                        content += (f"\n\n![image]({res['url']})\n\n{res['width']}x{res['height']} · "
                                    f"seed {res['seed']} · {res['seconds']}s\n\n{res['url']}")
        except Exception as e:
            content = f"出图时出错：{e}"

    body_out = {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "model": model}
    if body.get("stream"):
        return Response(sse(body_out, model, content), media_type="text/event-stream",
                        headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
    return {
        "id": body_out["id"], "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "elapsed": round(time.time() - started, 1),
    }


@app.api_route("/v1/images/{name}", methods=["GET"])
@app.api_route("/images/{name}", methods=["GET"])
async def proxy_image(name: str, request: Request):
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{STUDIO}/images/{name}")
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "image/png"))


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
async def passthrough_any(path: str, request: Request):
    raw = await request.body()
    return await forward(request, path, raw)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AUTO_PORT", "8090")), log_level="info")
