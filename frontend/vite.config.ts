import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { execSync } from 'node:child_process'
import { createReadStream, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// https://vite.dev/config/
const apiProxyTarget = 'http://127.0.0.1:8001'
const frontendRoot = path.dirname(fileURLToPath(import.meta.url))

/*
 * 結果ファイルに残すビルド識別子。どのコードで計算した結果かを後から
 * 辿れるよう、コミットの短縮ハッシュ（未コミット変更があれば -dirty）を
 * 埋め込む。git の無い環境でのビルドでは 'unknown' に落とす。
 */
function buildIdentifier(): string {
  try {
    const commit = execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString()
      .trim()
    const dirty = execSync('git status --porcelain', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString()
      .trim() !== ''
    return dirty ? `${commit}-dirty` : commit
  } catch {
    return 'unknown'
  }
}

/*
 * 攻撃シミュレーターのスクリプト実行は、Worker の中の Python（Pyodide）と
 * SharedArrayBuffer を使う。SharedArrayBuffer は cross-origin isolated なページでしか
 * 使えないため、開発サーバーでも配信と同じ COOP/COEP を付ける。
 * デスクトップ版の配信（api/main.py）も同じヘッダを付ける。
 */
const CROSS_ORIGIN_ISOLATION = {
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Embedder-Policy': 'require-corp',
}

/*
 * Pyodide 本体（JS・wasm・標準ライブラリ・lock）は npm パッケージから取り出し、
 * /assets/python-runtime/ にそのままの名前で置く。バンドルには通さない
 * （Pyodide は自分の場所を基準に残りのファイルを読む）。
 */
const PYTHON_RUNTIME_PREFIX = '/assets/python-runtime/'
const PYODIDE_FILES: Record<string, string> = {
  'pyodide.mjs': 'text/javascript',
  'pyodide.asm.mjs': 'text/javascript',
  'pyodide.asm.wasm': 'application/wasm',
  'python_stdlib.zip': 'application/zip',
  'pyodide-lock.json': 'application/json',
}
const pyodideDirectory = path.join(frontendRoot, 'node_modules', 'pyodide')
const pyodideVersion = (JSON.parse(readFileSync(path.join(pyodideDirectory, 'package.json'), 'utf8')) as { version: string }).version

function pythonRuntime(): Plugin {
  return {
    name: 'python-runtime',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const name = request.url?.split('?')[0].slice(PYTHON_RUNTIME_PREFIX.length)
        if (!request.url?.startsWith(PYTHON_RUNTIME_PREFIX) || !name || !(name in PYODIDE_FILES)) { next(); return }
        response.setHeader('Content-Type', PYODIDE_FILES[name])
        for (const [header, value] of Object.entries(CROSS_ORIGIN_ISOLATION)) response.setHeader(header, value)
        createReadStream(path.join(pyodideDirectory, name)).pipe(response)
      })
    },
    generateBundle() {
      for (const name of Object.keys(PYODIDE_FILES)) {
        this.emitFile({ type: 'asset', fileName: `${PYTHON_RUNTIME_PREFIX.slice(1)}${name}`,
          source: readFileSync(path.join(pyodideDirectory, name)) })
      }
    },
  }
}

export default defineConfig(() => ({
  plugins: [react(), pythonRuntime()],
  define: {
    __APP_BUILD__: JSON.stringify(buildIdentifier()),
    __PYODIDE_VERSION__: JSON.stringify(pyodideVersion),
  },
  worker: {
    format: 'es' as const,
    // Python の Worker は CSP を緩めて配信するため、置き場所を分けておく（api/main.py）。
    rolldownOptions: { output: { entryFileNames: 'assets/python-runtime/[name]-[hash].js',
      chunkFileNames: 'assets/python-runtime/[name]-[hash].js' } },
  },
  server: {
    headers: CROSS_ORIGIN_ISOLATION,
    // スクリプト実行の SDK は、手元のPython用の attack_client をそのまま読み込む。
    fs: { allow: [frontendRoot, path.join(frontendRoot, '..', 'attack_client'), path.join(frontendRoot, '..', 'examples')] },
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: { headers: CROSS_ORIGIN_ISOLATION },
}))
