// Qwen-Image-2.1 studio tool: submits a ComfyUI graph on the local DGX Spark and saves the PNG.
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export const name = 'qwen-studio'
export const inject = ['tools', 'workingDirectory']

const DEFAULT_SERVER = 'http://192.168.2.208:8188'
const TURBO_UNET = 'qwen_image_2.1_turbo_int8_convrot.safetensors'
const BASE_UNET = 'qwen_image_2.1_int8_convrot.safetensors'
const CLIP = 'qwen3vl_8b_int8_convrot.safetensors'
const VAE = 'qwen_image_2.1_vae_bf16.safetensors'
const TURBO_SIGMAS = '1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0'

function buildGraph({ prompt, width, height, seed, quality }) {
  const turbo = quality !== 'base'
  const g = {
    1: { class_type: 'UNETLoader', inputs: { unet_name: turbo ? TURBO_UNET : BASE_UNET, weight_dtype: 'default' } },
    2: { class_type: 'CLIPLoader', inputs: { clip_name: CLIP, type: 'qwen_image', device: 'default' } },
    3: { class_type: 'VAELoader', inputs: { vae_name: VAE } },
    4: { class_type: 'CLIPTextEncode', inputs: { clip: ['2', 0], text: prompt } },
    6: { class_type: 'EmptySD3LatentImage', inputs: { width, height, batch_size: 1 } },
    9: { class_type: 'VAEDecode', inputs: { samples: ['8', 0], vae: ['3', 0] } },
    10: { class_type: 'SaveImage', inputs: { images: ['9', 0], filename_prefix: 'studio/IMG' } },
  }
  if (turbo) {
    g[11] = { class_type: 'BasicGuider', inputs: { model: ['1', 0], conditioning: ['4', 0] } }
    g[12] = { class_type: 'ManualSigmas', inputs: { sigmas: TURBO_SIGMAS } }
    g[13] = { class_type: 'KSamplerSelect', inputs: { sampler_name: 'euler' } }
    g[14] = { class_type: 'RandomNoise', inputs: { noise_seed: seed } }
    g[8] = {
      class_type: 'SamplerCustomAdvanced',
      inputs: { noise: ['14', 0], guider: ['11', 0], sampler: ['13', 0], sigmas: ['12', 0], latent_image: ['6', 0] },
    }
  } else {
    g[5] = { class_type: 'CLIPTextEncode', inputs: { clip: ['2', 0], text: '' } }
    g[7] = { class_type: 'ModelSamplingAuraFlow', inputs: { model: ['1', 0], shift: 3.1 } }
    g[8] = {
      class_type: 'KSampler',
      inputs: {
        model: ['7', 0], positive: ['4', 0], negative: ['5', 0], latent_image: ['6', 0],
        seed, steps: 20, cfg: 2.5, sampler_name: 'euler', scheduler: 'simple', denoise: 1.0,
      },
    }
  }
  return g
}

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) throw new Error(`studio ${res.status} at ${url}: ${text.slice(0, 300)}`)
  return JSON.parse(text)
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms)
    signal?.addEventListener('abort', () => {
      clearTimeout(t)
      reject(new Error('aborted'))
    }, { once: true })
  })
}

export function apply(ctx, config = {}) {
  const servers = (Array.isArray(config.servers) && config.servers.length
    ? config.servers
    : [config.server ?? DEFAULT_SERVER]).map((s) => String(s).replace(/\/+$/, ''))
  const outDirName = String(config.outputDir ?? 'studio')
  let cursor = 0

  ctx.effect(() => ctx.tools.register({
    name: 'studio_image',
    description:
      'Generate an image with Qwen-Image-2.1 on the local DGX Spark studio (ComfyUI on gua1, port 8188) and save the PNG into the workspace. ' +
      'Returns the saved path plus size and seed. One render takes roughly 15-60 s when the model is warm and up to a few minutes when it is cold.',
    parameters: {
      type: 'object',
      properties: {
        prompt: {
          type: 'string',
          description: 'Positive prompt. English works best; include subject, setting, lighting, lens and style.',
        },
        width: { type: 'integer', description: 'Pixel width, multiple of 16. Default 1328.', default: 1328 },
        height: { type: 'integer', description: 'Pixel height, multiple of 16. Default 1328.', default: 1328 },
        seed: { type: 'integer', description: 'Random seed; omit for a random one.' },
        quality: {
          type: 'string',
          enum: ['turbo', 'base'],
          default: 'turbo',
          description: 'turbo is the fast 8-step schedule; base is the full 20-step model at cfg 2.5.',
        },
      },
      required: ['prompt'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          width: { type: 'integer' },
          height: { type: 'integer' },
          seed: { type: 'integer' },
          seconds: { type: 'number' },
          promptId: { type: 'string' },
        },
        required: ['path', 'width', 'height', 'seed', 'seconds'],
        additionalProperties: true,
      },
      render: (_args, value) => [{
        type: 'text',
        text: `saved ${value.path} — ${value.width}x${value.height}, seed ${value.seed}, ${value.seconds}s on ${value.server ?? 'studio'}`,
      }],
    },
    timeoutMs: 900_000,
    async execute(args, exec) {
      const prompt = String(args?.prompt ?? '').trim()
      if (!prompt) throw new Error('prompt is required')
      const width = Number(args?.width ?? 1328)
      const height = Number(args?.height ?? 1328)
      const quality = args?.quality === 'base' ? 'base' : 'turbo'
      const seed = Number.isFinite(args?.seed) ? Number(args.seed) : Math.floor(Math.random() * 2_147_483_647)
      const started = Date.now()

      const graph = buildGraph({ prompt, width, height, seed, quality })
      const order = servers.map((_, i) => servers[(cursor + i) % servers.length])
      cursor = (cursor + 1) % servers.length

      let server = null
      let promptId = null
      let images = null
      let failure = null
      for (const candidate of order) {
        try {
          const body = await postJson(`${candidate}/prompt`, { prompt: graph })
          server = candidate
          promptId = body.prompt_id
          break
        } catch (error) {
          failure = `${candidate}: ${error.message}`
        }
      }
      if (!server) throw new Error(`no studio accepted the job: ${failure}`)

      const deadline = started + 840_000
      while (Date.now() < deadline && !exec.signal?.aborted) {
        await sleep(1500, exec.signal)
        const res = await fetch(`${server}/history/${promptId}`)
        if (!res.ok) continue
        const hist = await res.json()
        const entry = hist?.[promptId]
        if (!entry) continue
        const status = entry.status ?? {}
        if (status.status_str === 'error') {
          for (const [kind, data] of status.messages ?? []) {
            if (kind === 'execution_error') failure = `${data.exception_type}: ${data.exception_message}`
          }
          break
        }
        const found = []
        for (const node of Object.values(entry.outputs ?? {})) found.push(...(node.images ?? []))
        if (found.length) {
          images = found
          break
        }
      }
      if (failure) throw new Error(`studio render failed: ${failure}`)
      if (!images?.length) throw new Error('studio did not return an image (timeout or empty history)')

      const img = images[0]
      const q = new URLSearchParams({
        filename: img.filename,
        subfolder: img.subfolder ?? '',
        type: img.type ?? 'output',
      })
      const bin = await fetch(`${server}/view?${q}`)
      if (!bin.ok) throw new Error(`studio view ${bin.status}`)
      const bytes = Buffer.from(await bin.arrayBuffer())

      let base = process.cwd()
      try {
        base = await ctx.workingDirectory.ensure(exec.agent, exec.signal)
      } catch {}
      const dir = join(base, outDirName)
      await mkdir(dir, { recursive: true })
      const file = join(dir, `${new Date(started).toISOString().replace(/[:.]/g, '-')}-${seed}.png`)
      await writeFile(file, bytes)

      return {
        path: file,
        width,
        height,
        seed,
        seconds: Math.round((Date.now() - started) / 100) / 10,
        promptId,
        server,
        bytes: bytes.length,
      }
    },
  }), 'qwen-studio: studio_image')
}
