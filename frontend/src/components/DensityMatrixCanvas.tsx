import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import './DensityMatrixCanvas.css'
import {
  densityCellAt,
  type DensityMatrixCell,
  type DensityMatrixMode,
  type ValidatedDensityMatrix,
} from '../utils/densityMatrix'
import { phaseBand, phaseBandRgb, type Rgb } from '../utils/phaseColor'

/*
 * 密度行列をGPUで1枚の画像として描く。
 *
 * DOMのセル格子は5量子ビット(32x32=1024セル)あたりで破綻するが、
 * 8量子ビットでも 256x256 のテクスチャ1枚にすぎない。読むものが
 * 「個々の数値」から「模様」に変わるので、コヒーレンスのブロック構造や
 * デコヒーレンスの広がりはむしろこちらの方が見える。
 *
 * 連続階調は使わない。順序ディザ＋ポスタリゼーションで、
 * 印刷物のハーフトーンに寄せる（skill §7）。
 */

const MODE_INDEX: Record<DensityMatrixMode, number> = {
  magnitude: 0,
  real: 1,
  imaginary: 2,
  phase: 3,
}

/* 階調数。増やすほど滑らかになるので、あえて低く保つ。 */
const POSTERIZE_LEVELS = 6

const VERTEX_SHADER = `#version 300 es
in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`

const FRAGMENT_SHADER = `#version 300 es
precision highp float;

uniform sampler2D uField;
uniform float uDimension;
uniform int uMode;
uniform float uDenominator;
uniform vec2 uHover;
uniform float uCellPx;
uniform float uLevels;
uniform vec3 uVoid;
uniform vec3 uInk;
uniform vec3 uAccent;
uniform vec3 uAmber;
uniform vec3 uBlue;

in vec2 vUv;
out vec4 outColor;

/* 4x4 順序ディザ。スクリーン空間で当てて網点に見せる。 */
float bayer4(vec2 p) {
  float m[16] = float[16](
     0.0,  8.0,  2.0, 10.0,
    12.0,  4.0, 14.0,  6.0,
     3.0, 11.0,  1.0,  9.0,
    15.0,  7.0, 13.0,  5.0
  );
  int x = int(mod(p.x, 4.0));
  int y = int(mod(p.y, 4.0));
  return m[y * 4 + x] / 16.0;
}

/* utils/phaseColor.ts と同じ循環ランプ: 0=インク, +π/2=橙, ±π=赤, -π/2=青。 */
vec3 phaseAnchor(int i) {
  if (i == 0) return uInk;
  if (i == 1) return uAmber;
  if (i == 2) return uAccent;
  return uBlue;
}

void main() {
  vec2 uv = vec2(vUv.x, 1.0 - vUv.y);
  vec2 cell = clamp(floor(uv * uDimension), vec2(0.0), vec2(uDimension - 1.0));

  vec2 data = texture(uField, (cell + 0.5) / uDimension).rg;
  float signedValue = uDenominator > 0.0 ? clamp(data.r / uDenominator, -1.0, 1.0) : 0.0;
  float level = clamp(data.g, 0.0, 1.0);

  vec3 tint;
  if (uMode == 0) {
    tint = uInk;
  } else if (uMode == 3) {
    /*
     * 位相は循環量なので一周で元の色に戻るランプを使う。8段に量子化し、
     * ±pi が同じ帯に入るよう帯の中心を 0, pi/4, ... に置く。
     */
    float band = mod(floor(signedValue * 4.0 + 0.5), 8.0);
    float segment = band * 0.5;
    int lower = int(floor(segment));
    tint = mix(phaseAnchor(lower), phaseAnchor((lower + 1) % 4), segment - floor(segment));
  } else {
    /* 実部・虚部は符号で二色に分ける。全画面共通で 正=赤, 負=青。 */
    tint = signedValue < 0.0 ? uBlue : uAccent;
  }

  /*
   * 標準的な順序ディザ: 階調境界をディザで跨がせてから量子化する。
   * floor(level*steps + dither) / steps なので明度に偏りが乗らない。
   */
  float dither = bayer4(gl_FragCoord.xy);
  float steps = max(uLevels - 1.0, 1.0);
  float quantised = clamp(floor(level * steps + dither), 0.0, steps) / steps;
  vec3 color = mix(uVoid, tint, quantised);

  vec2 withinCell = fract(uv * uDimension);

  /* セルが十分大きいときだけ罫線を引く。 */
  if (uCellPx >= 7.0) {
    float ruleWidth = 1.0 / uCellPx;
    if (withinCell.x < ruleWidth || withinCell.y < ruleWidth) {
      color = mix(color, uVoid, 0.55);
    }
  }

  /* 注目セルの枠。正の値の赤と紛れないよう、インクの外枠＋地色の内枠にする。 */
  if (uHover.x >= 0.0
      && abs(cell.x - uHover.x) < 0.5
      && abs(cell.y - uHover.y) < 0.5) {
    float edge = max(2.0 / uCellPx, 0.12);
    float distanceToEdge = min(min(withinCell.x, 1.0 - withinCell.x), min(withinCell.y, 1.0 - withinCell.y));
    if (distanceToEdge < edge) {
      color = distanceToEdge < edge * 0.5 ? uInk : uVoid;
    }
  }

  outColor = vec4(color, 1.0);
}`

function readToken(name: string, fallback: Rgb): Rgb {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  const match = /^#?([0-9a-f]{6})$/i.exec(raw)
  if (match === null) {
    return fallback
  }
  const int = Number.parseInt(match[1], 16)
  return [((int >> 16) & 255) / 255, ((int >> 8) & 255) / 255, (int & 255) / 255]
}

/* `--gate-*-rgb` のような "R G B" 形式のトークンを読む。 */
function readRgbToken(name: string, fallback: Rgb): Rgb {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  const parts = raw.split(/\s+/).map(Number)
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) {
    return fallback
  }
  return [parts[0] / 255, parts[1] / 255, parts[2] / 255]
}

const AMBER_FALLBACK: Rgb = [245 / 255, 165 / 255, 36 / 255]
const BLUE_FALLBACK: Rgb = [74 / 255, 168 / 255, 255 / 255]

function compile(gl: WebGL2RenderingContext, type: number, source: string) {
  const shader = gl.createShader(type)
  if (shader === null) {
    return null
  }
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.error('density matrix shader failed:', gl.getShaderInfoLog(shader))
    gl.deleteShader(shader)
    return null
  }
  return shader
}

type GlRuntime = {
  gl: WebGL2RenderingContext
  program: WebGLProgram
  texture: WebGLTexture
  vao: WebGLVertexArrayObject
  uniforms: Record<string, WebGLUniformLocation | null>
}

function initGl(canvas: HTMLCanvasElement): GlRuntime | null {
  const gl = canvas.getContext('webgl2', {
    antialias: false,
    alpha: false,
    preserveDrawingBuffer: false,
  })
  if (gl === null) {
    return null
  }

  const vertexShader = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER)
  const fragmentShader = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER)
  const program = gl.createProgram()
  if (vertexShader === null || fragmentShader === null || program === null) {
    return null
  }

  gl.attachShader(program, vertexShader)
  gl.attachShader(program, fragmentShader)
  gl.linkProgram(program)
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.error('density matrix program failed:', gl.getProgramInfoLog(program))
    return null
  }
  gl.deleteShader(vertexShader)
  gl.deleteShader(fragmentShader)

  const vao = gl.createVertexArray()
  const buffer = gl.createBuffer()
  const texture = gl.createTexture()
  if (vao === null || buffer === null || texture === null) {
    return null
  }

  gl.bindVertexArray(vao)
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
  gl.bufferData(
    gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 3, -1, -1, 3]),
    gl.STATIC_DRAW,
  )
  const position = gl.getAttribLocation(program, 'aPos')
  gl.enableVertexAttribArray(position)
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0)
  gl.bindVertexArray(null)

  const uniformNames = [
    'uField', 'uDimension', 'uMode', 'uDenominator',
    'uHover', 'uCellPx', 'uLevels', 'uVoid', 'uInk', 'uAccent',
    'uAmber', 'uBlue',
  ]
  const uniforms: Record<string, WebGLUniformLocation | null> = {}
  for (const name of uniformNames) {
    uniforms[name] = gl.getUniformLocation(program, name)
  }

  return { gl, program, texture, vao, uniforms }
}

/* value と intensity を1枚のRG32Fテクスチャにまとめる。 */
function packField(matrix: ValidatedDensityMatrix): Float32Array {
  const { dimension, field } = matrix
  const packed = new Float32Array(dimension * dimension * 2)
  for (let index = 0; index < dimension * dimension; index += 1) {
    packed[index * 2] = field.value[index]
    packed[index * 2 + 1] = field.intensity[index]
  }
  return packed
}

type DensityMatrixCanvasProps = {
  matrix: ValidatedDensityMatrix
  mode: DensityMatrixMode
  selectedCell?: { row: number; column: number } | null
  onInspect: (cell: DensityMatrixCell | null) => void
}

export function DensityMatrixCanvas({
  matrix,
  mode,
  selectedCell = null,
  onInspect,
}: DensityMatrixCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const runtimeRef = useRef<GlRuntime | null>(null)
  /* 2D経路の下絵。ホバーのたびに描き直さないようキャッシュする。 */
  const offscreenRef = useRef<HTMLCanvasElement | null>(null)
  const [cursor, setCursor] = useState<{ row: number; column: number } | null>(null)
  const [glFailed, setGlFailed] = useState(false)
  const [glGeneration, setGlGeneration] = useState(0)

  /*
   * canvas の context 種別は一度決まると変更できない。WebGL2 を張ってしまうと
   * 後から 2D にフォールバックできなくなるので、使い捨ての canvas で先に
   * 対応可否だけを判定し、本番の canvas には片方しか張らない。
   */
  const [supportsGl] = useState(() => {
    try {
      return document.createElement('canvas').getContext('webgl2') !== null
    } catch {
      return false
    }
  })
  const usesFallback = !supportsGl || glFailed

  const { dimension } = matrix

  /* --- 2D フォールバック。WebGL2 が無い環境でも同じ絵を出す。 ------------- */

  /* 下絵をオフスクリーンに焼く。データかサイズが変わったときだけ走る。 */
  const paintOffscreen = useCallback(
    (width: number, height: number) => {
      let offscreen = offscreenRef.current
      if (offscreen === null) {
        offscreen = document.createElement('canvas')
        offscreenRef.current = offscreen
      }
      offscreen.width = width
      offscreen.height = height
      const context = offscreen.getContext('2d')
      if (context === null) {
        return null
      }
      const image = context.createImageData(width, height)
      const bayer = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5]
      const paint = readToken('--tt-void', [0.04, 0.04, 0.04])
      const ink = readToken('--tt-ink', [0.92, 0.92, 0.92])
      const accent = readToken('--tt-accent', [0.9, 0.1, 0.1])
      const phaseAnchors: [Rgb, Rgb, Rgb, Rgb] = [
        ink,
        readRgbToken('--gate-phase-rgb', AMBER_FALLBACK),
        accent,
        readRgbToken('--gate-rotation-rgb', BLUE_FALLBACK),
      ]

      for (let y = 0; y < height; y += 1) {
        const row = Math.min(Math.floor((y / height) * dimension), dimension - 1)
        for (let x = 0; x < width; x += 1) {
          const column = Math.min(Math.floor((x / width) * dimension), dimension - 1)
          const index = row * dimension + column
          const signed = matrix.denominator > 0
            ? Math.max(-1, Math.min(matrix.field.value[index] / matrix.denominator, 1))
            : 0
          const level = Math.max(0, Math.min(matrix.field.intensity[index], 1))

          let tint = ink
          if (mode === 'phase') {
            tint = phaseBandRgb(phaseBand(signed * Math.PI), phaseAnchors)
          } else if (mode !== 'magnitude') {
            tint = signed < 0 ? phaseAnchors[3] : accent
          }

          const dither = bayer[(y % 4) * 4 + (x % 4)] / 16
          const steps = POSTERIZE_LEVELS - 1
          const q = Math.max(0, Math.min(Math.floor(level * steps + dither), steps)) / steps

          const offset = (y * width + x) * 4
          for (let channel = 0; channel < 3; channel += 1) {
            const value = paint[channel] + (tint[channel] - paint[channel]) * q
            image.data[offset + channel] = Math.round(value * 255)
          }
          image.data[offset + 3] = 255
        }
      }
      context.putImageData(image, 0, 0)
      return offscreen
    },
    [dimension, matrix, mode],
  )

  /* 下絵を貼り、注目セルの枠だけを上から描く。 */
  const drawFallback = useCallback(
    (canvas: HTMLCanvasElement, hover: { row: number; column: number } | null) => {
      const context = canvas.getContext('2d')
      if (context === null) {
        return
      }
      const { width, height } = canvas
      let offscreen = offscreenRef.current
      if (offscreen === null || offscreen.width !== width || offscreen.height !== height) {
        offscreen = paintOffscreen(width, height)
      }
      if (offscreen === null) {
        return
      }

      context.imageSmoothingEnabled = false
      context.drawImage(offscreen, 0, 0)

      if (hover !== null) {
        const cellWidth = width / dimension
        const cellHeight = height / dimension
        const toCss = (rgb: Rgb) => `rgb(${rgb.map((channel) => Math.round(channel * 255)).join(',')})`
        const line = Math.max(1, Math.min(cellWidth, cellHeight) * 0.06)
        /* GPU 版と同じく、インクの外枠＋地色の内枠。 */
        context.lineWidth = line
        context.strokeStyle = toCss(readToken('--tt-ink', [0.92, 0.92, 0.92]))
        context.strokeRect(
          hover.column * cellWidth + line / 2,
          hover.row * cellHeight + line / 2,
          cellWidth - line,
          cellHeight - line,
        )
        context.strokeStyle = toCss(readToken('--tt-void', [0.04, 0.04, 0.04]))
        context.strokeRect(
          hover.column * cellWidth + line * 1.5,
          hover.row * cellHeight + line * 1.5,
          cellWidth - line * 3,
          cellHeight - line * 3,
        )
      }
    },
    [dimension, paintOffscreen],
  )

  const render = useCallback(
    (hover: { row: number; column: number } | null) => {
      const canvas = canvasRef.current
      if (canvas === null) {
        return
      }

      const runtime = runtimeRef.current
      if (runtime === null) {
        drawFallback(canvas, hover)
        return
      }

      const { gl, program, texture, vao, uniforms } = runtime
      gl.viewport(0, 0, canvas.width, canvas.height)
      gl.useProgram(program)
      gl.bindVertexArray(vao)

      gl.activeTexture(gl.TEXTURE0)
      gl.bindTexture(gl.TEXTURE_2D, texture)
      gl.uniform1i(uniforms.uField, 0)
      gl.uniform1f(uniforms.uDimension, dimension)
      gl.uniform1i(uniforms.uMode, MODE_INDEX[mode])
      gl.uniform1f(uniforms.uDenominator, matrix.denominator)
      gl.uniform1f(uniforms.uCellPx, canvas.width / dimension)
      gl.uniform1f(uniforms.uLevels, POSTERIZE_LEVELS)
      gl.uniform2f(
        uniforms.uHover,
        hover === null ? -1 : hover.column,
        hover === null ? -1 : hover.row,
      )
      gl.uniform3fv(uniforms.uVoid, readToken('--tt-void', [0.04, 0.04, 0.04]))
      gl.uniform3fv(uniforms.uInk, readToken('--tt-ink', [0.92, 0.92, 0.92]))
      gl.uniform3fv(uniforms.uAccent, readToken('--tt-accent', [0.9, 0.1, 0.1]))
      gl.uniform3fv(uniforms.uAmber, readRgbToken('--gate-phase-rgb', AMBER_FALLBACK))
      gl.uniform3fv(uniforms.uBlue, readRgbToken('--gate-rotation-rgb', BLUE_FALLBACK))

      gl.drawArrays(gl.TRIANGLES, 0, 3)
    },
    [dimension, drawFallback, matrix.denominator, mode],
  )

  /* データかモードが変われば下絵は無効。 */
  useEffect(() => {
    offscreenRef.current = null
  }, [matrix, mode])

  /*
   * WebGL 資源の確保。コンテキスト自体は破棄しない——StrictMode の
   * 二重マウントで loseContext すると、再取得したコンテキストが死んだままになる。
   */
  useEffect(() => {
    const canvas = canvasRef.current
    if (!supportsGl || canvas === null) {
      return
    }

    const runtime = initGl(canvas)
    runtimeRef.current = runtime
    if (runtime === null) {
      setGlFailed(true)
      return
    }
    setGlGeneration((generation) => generation + 1)

    return () => {
      const { gl, program, texture, vao } = runtime
      gl.deleteProgram(program)
      gl.deleteTexture(texture)
      gl.deleteVertexArray(vao)
      runtimeRef.current = null
    }
  }, [supportsGl])

  /* データ更新のたびにテクスチャを差し替える。再初期化後にも張り直す。 */
  useEffect(() => {
    const runtime = runtimeRef.current
    if (runtime === null) {
      return
    }
    void glGeneration
    const { gl, texture } = runtime
    gl.bindTexture(gl.TEXTURE_2D, texture)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    gl.texImage2D(
      gl.TEXTURE_2D, 0, gl.RG32F,
      dimension, dimension, 0,
      gl.RG, gl.FLOAT, packField(matrix),
    )
  }, [dimension, matrix, glGeneration])

  /*
   * リサイズ監視は張りっぱなしにしたいので、最新の描画呼び出しは ref 経由で
   * 参照する。cursor を依存に入れるとホバーのたびに Observer が張り直される。
   */
  const redrawRef = useRef<() => void>(() => {})
  useEffect(() => {
    redrawRef.current = () => render(cursor ?? selectedCell)
  }, [cursor, render, selectedCell])

  /* 表示サイズに追従。 */
  useEffect(() => {
    const canvas = canvasRef.current
    const container = canvas?.parentElement
    if (!canvas || !container) {
      return
    }

    function resize() {
      if (!canvas || !container) {
        return
      }
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      const cssSize = Math.max(160, Math.min(container.clientWidth, 640))
      const pixels = Math.round(cssSize * ratio)
      canvas.style.width = `${cssSize}px`
      canvas.style.height = `${cssSize}px`
      canvas.width = pixels
      canvas.height = pixels
      redrawRef.current()
    }

    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    render(cursor ?? selectedCell)
  }, [cursor, render, selectedCell])

  function cellFromPointer(event: ReactPointerEvent<HTMLCanvasElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    const column = Math.floor(((event.clientX - rect.left) / rect.width) * dimension)
    const row = Math.floor(((event.clientY - rect.top) / rect.height) * dimension)
    if (row < 0 || column < 0 || row >= dimension || column >= dimension) {
      return null
    }
    return { row, column }
  }

  function moveCursor(rowDelta: number, columnDelta: number) {
    const base = cursor ?? selectedCell ?? { row: 0, column: 0 }
    const next = {
      row: Math.max(0, Math.min(base.row + rowDelta, dimension - 1)),
      column: Math.max(0, Math.min(base.column + columnDelta, dimension - 1)),
    }
    setCursor(next)
    onInspect(densityCellAt(matrix, next.row, next.column))
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLCanvasElement>) {
    const moves: Record<string, [number, number]> = {
      ArrowUp: [-1, 0],
      ArrowDown: [1, 0],
      ArrowLeft: [0, -1],
      ArrowRight: [0, 1],
    }
    const move = moves[event.key]
    if (move === undefined) {
      return
    }
    event.preventDefault()
    moveCursor(move[0], move[1])
  }

  return (
    <div className="density-matrix-canvas">
      <canvas
        ref={canvasRef}
        className="density-matrix-canvas__surface"
        tabIndex={0}
        role="img"
        aria-label={`${dimension}x${dimension} 密度行列ヒートマップ。矢印キーでセルを選択できます。`}
        onPointerMove={(event) => {
          const next = cellFromPointer(event)
          setCursor(next)
          onInspect(next === null ? null : densityCellAt(matrix, next.row, next.column))
        }}
        onPointerLeave={() => {
          setCursor(null)
          onInspect(null)
        }}
        onKeyDown={handleKeyDown}
      />
      <div className="density-matrix-canvas__axes" aria-hidden="true">
        <span>{matrix.labels[0]}</span>
        <span>{matrix.labels[dimension - 1]}</span>
      </div>
      {usesFallback ? (
        <p className="density-matrix-canvas__notice">
          WebGL2 が利用できないため、CPU描画にフォールバックしています。
        </p>
      ) : null}
      <p className="density-matrix-canvas__hint">
        {cursor === null
          ? selectedCell === null
            ? 'カーソルを合わせるか、フォーカスして矢印キーでセルを調べられます。'
            : `検索位置: 行 ${matrix.labels[selectedCell.row]} / 列 ${matrix.labels[selectedCell.column]}`
          : `行 ${matrix.labels[cursor.row]} / 列 ${matrix.labels[cursor.column]}`}
      </p>
    </div>
  )
}
