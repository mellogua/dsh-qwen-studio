#!/usr/bin/env python3
"""Minimal ComfyUI client for the Qwen-Image-2.1 studio (stdlib only).

usage: test_studio.py "prompt" [width] [height] [seed] [outfile]
"""
import json
import sys
import time
import urllib.request
import urllib.error

HOST = "http://127.0.0.1:8188"
UNET_TURBO = "qwen_image_2.1_turbo_int8_convrot.safetensors"
UNET = "qwen_image_2.1_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0"


def graph(prompt, width, height, seed, turbo=True):
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_TURBO if turbo else UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "studio/IMG"}},
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


def post(path, payload):
    req = urllib.request.Request(HOST + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "a red apple on a white table"
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1024
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 1024
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 42
    out = sys.argv[5] if len(sys.argv) > 5 else "/tmp/studio-test.png"

    print("queueing", width, "x", height, "seed", seed)
    resp = post("/prompt", {"prompt": graph(prompt, width, height, seed)})
    pid = resp["prompt_id"]
    print("prompt_id", pid)
    t0 = time.time()
    while time.time() - t0 < 900:
        time.sleep(2)
        try:
            with urllib.request.urlopen(f"{HOST}/history/{pid}", timeout=30) as r:
                hist = json.load(r)
        except urllib.error.URLError:
            continue
        if pid in hist:
            entry = hist[pid]
            st = entry.get("status", {})
            if st.get("status_str") == "error":
                for kind, data in st.get("messages", []):
                    if kind == "execution_error":
                        print("EXEC ERROR:", data.get("exception_type"), data.get("exception_message", "")[:400])
                print("error, no image")
                return 1
            imgs = []
            for node in entry.get("outputs", {}).values():
                imgs += node.get("images", [])
            if imgs:
                img = imgs[0]
                q = urllib.parse.urlencode({"filename": img["filename"],
                                            "subfolder": img.get("subfolder", ""),
                                            "type": img.get("type", "output")})
                with urllib.request.urlopen(f"{HOST}/view?{q}", timeout=120) as r:
                    data = r.read()
                with open(out, "wb") as f:
                    f.write(data)
                print(f"saved {out} {len(data)}B in {time.time()-t0:.1f}s")
                return 0
            print("status:", json.dumps(entry.get("status", {}))[:300])
    print("timeout")
    return 1


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
