import { useEffect, useRef } from 'react'
import './QuantumFluctuationField.css'
import { useAnimationSettings } from '../context/useAnimationSettings'
import { useTheme } from '../context/useTheme'
import { usePrefersReducedMotion } from '../hooks/usePrefersReducedMotion'
import type { ModeId } from '../utils/homeModes'

/*
 * ホーム画面の下地に敷く「真空のゆらぎ」。
 *
 * ただの装飾ノイズにはしない。このアプリが扱っているのは開放量子系なので、
 * 背景も同じ言葉で書く。描いているのは2つ。
 *
 *   1. 自由スカラー場の零点振動
 *      φ(x,t) = Σ_k a_k cos(k·x − ω_k t + θ_k)
 *      分散関係は ω_k = √(|k|² + m²)（c = ħ = 1）。各モードの振幅は
 *      真空の零点振幅 a_k ∝ 1/√(2ω_k) を取る。つまり長波長ほど強く出る。
 *      白色ノイズではなく赤方に傾いた模様になるのは、この重みのため。
 *
 *   2. 仮想対の生成と消滅
 *      不確定性関係 ΔE·Δt ≳ ħ/2 の範囲でだけ存在できる粒子・反粒子の対。
 *      対の広がり（≒ コンプトン波長 ħ/mc）が大きいほど ΔE = 2mc² は小さく、
 *      許される寿命 Δt は長い。だから「大きい対はゆっくり、小さい対は一瞬」。
 *
 * さらに、正面に出ているモードによって場の「相」が移り変わる。切り替えるのは
 * 質量 m と時間スケールで、模様の粗さ・速さ・赤の出方はすべてそこから従属して
 * 決まる（下の REGIMES を参照）。相の間は指数緩和でつなぐので、ドラムが
 * 回りきるより少し遅れて背景が追いついてくる。
 *
 * 描画は低解像度（GRID_COLS 幅）のオフスクリーンに場を書いてから拡大する。
 * 場の評価は加法定理で行方向・列方向に分離してあるので、1フレームあたりの
 * 三角関数呼び出しはセル数ではなくモード数 ×(cols + rows) で済む。
 */

/* 重ね合わせるモードの本数。増やすほど模様が細かく、のっぺりする。 */
const MODE_COUNT = 9

/* 場を評価する格子の横方向セル数。縦は画面のアスペクト比から決める。 */
const GRID_COLS = 120
const MIN_ROWS = 44
const MAX_ROWS = 200

/* 連続階調は使わない。等高線のように段を作る（index.css の版下感に合わせる）。 */
const POSTERIZE_LEVELS = 5

/* 空間周波数の上限。2π × n で n 波長ぶん画面に入る。 */
const MAX_WAVENUMBER = 2 * Math.PI * 2.4

/* 同時に存在させる仮想対の数。 */
const PAIR_COUNT = 9

/* 対の広がり（css px）。寿命はこれに比例する（Δt ∝ ħ/ΔE ∝ 広がり）。 */
const PAIR_MIN_REACH = 26
const PAIR_MAX_REACH = 190
const PAIR_LIFETIME_PER_PX = 0.0135

/*
 * 背景なので描画は 30fps で足りる。作業画面ではさらに落とす。あちらは
 * 重いキャンバスが同時に何枚も回っているので、下地に払う予算は少ない方がいい。
 */
const FEATURE_FRAME_INTERVAL_MS = 1000 / 30
const AMBIENT_FRAME_INTERVAL_MS = 1000 / 20

/*
 * 下地としての出しゃばり具合。
 *   feature … ホームと、Gate-aware / Pulse の作業画面。作業画面では中央の
 *             データパネルが場を覆うので、余白で見える分はホームと同じ濃さで出す。
 *   ambient … それ以外（ヘルプ）。データを読む邪魔にならないところまで引く。
 */
const PRESENCE_LEVELS = {
  feature: 1,
  ambient: 0.38,
} as const

export type FieldPresence = keyof typeof PRESENCE_LEVELS

/* タブを離れて戻ったときに位相が飛ばないよう、1フレームの刻みを頭打ちにする。 */
const MAX_TIME_STEP = 0.1

/* 相の乗り換えにかける時定数（秒）。ドラムの回転 900ms より少し長く取る。 */
const PHASE_RELAXATION = 0.85

type FieldRegime = {
  /*
   * 質量 m。ω = √(k²+m²) と a = 1/√(2ω) の両方に効くので、これ1つで
   * 模様の粗さと速さの関係が決まる。
   *   m が小さい … ω ≈ |k|。長波長ほど振幅が大きく、かつゆっくり。
   *                大きく滑らかな塊がのそのそ動く。
   *   m が大きい … ω ≈ m。どのモードもほぼ同じ振幅・同じ速さになり、
   *                スペクトルが白に寄る。細かい粒が一斉に震える。
   */
  mass: number
  /*
   * k 全体にかける倍率。大きいほど短波長＝細かい模様になる。連続的に
   * 動かすので、相の乗り換えでは模様が伸び縮みしながら入れ替わる。
   */
  wavenumberScale: number
  /* 実時間 → 場の時間。小さいほどゆっくり漂う。 */
  timeScale: number
  /* 仮想対の回転の速さ。エネルギー密度が高いほど寿命は短い。 */
  pairRate: number
  /* この値より深い負の谷だけをアクセント色に振る。低いほど赤が増える。 */
  troughThreshold: number
  /* 場全体の濃さの倍率。 */
  intensity: number
}

/*
 * 面ごとの相。中身の性格をそのまま場の物理量に翻訳している。
 *   物理エンジン … 基準。落ち着いているが死んではいない。
 *   攻撃      … 物理エンジンより谷を浅く拾い、赤が多めに出る。
 *   ドキュメント … k も時間も落とす。大きな塊が凪いで、赤はほとんど出ない。
 */
const REGIMES: Record<ModeId, FieldRegime> = {
  physics: {
    mass: 0.9,
    wavenumberScale: 1,
    timeScale: 0.085,
    pairRate: 1,
    troughThreshold: 0.52,
    intensity: 1,
  },
  attack: {
    mass: 1.7,
    wavenumberScale: 1.3,
    timeScale: 0.17,
    pairRate: 1.6,
    troughThreshold: 0.3,
    intensity: 1.1,
  },
  docs: {
    mass: 0.3,
    wavenumberScale: 0.58,
    timeScale: 0.03,
    pairRate: 0.45,
    troughThreshold: 0.7,
    intensity: 0.82,
  },
}

type FieldMode = {
  waveNumberX: number
  waveNumberY: number
  /* |k|。ω は毎フレーム、そのときの質量から引き直す。 */
  magnitude: number
  /*
   * 累積位相。ω·t として毎フレーム計算し直すと、質量が動いた瞬間に位相が
   * 飛ぶ。dφ = ω·dt を足し込む形にして連続性を保つ。
   */
  phase: number
}

type VirtualPair = {
  centerX: number
  centerY: number
  directionX: number
  directionY: number
  reach: number
  lifetime: number
  bornAt: number
}

type Palette = {
  ink: [number, number, number]
  accent: [number, number, number]
  fieldAlpha: number
  pairAlpha: number
}

function readChannels(name: string, fallback: [number, number, number]): [number, number, number] {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  const parts = raw.split(/[\s,]+/).map(Number)
  if (parts.length < 3 || parts.some(Number.isNaN)) {
    return fallback
  }
  return [parts[0], parts[1], parts[2]]
}

function readPalette(isLight: boolean): Palette {
  return {
    ink: readChannels('--tt-ink-rgb', [234, 234, 234]),
    accent: readChannels('--tt-accent-rgb', [230, 25, 25]),
    /*
     * 明るい下地では同じ不透明度でも濃く見える。読字の邪魔にならない上限は
     * 実測でこのあたり。
     */
    fieldAlpha: isLight ? 0.15 : 0.24,
    pairAlpha: isLight ? 0.5 : 0.62,
  }
}

function createModes(): FieldMode[] {
  const modes: FieldMode[] = []
  for (let index = 0; index < MODE_COUNT; index += 1) {
    /*
     * k は等方に取る。ただし極端に長い波（|k| ≈ 0）は画面全体が一様に
     * 明滅するだけになるので、下限を切っておく。
     */
    const direction = Math.random() * Math.PI * 2
    const magnitude = MAX_WAVENUMBER * (0.22 + 0.78 * Math.random())

    modes.push({
      waveNumberX: Math.cos(direction) * magnitude,
      waveNumberY: Math.sin(direction) * magnitude,
      magnitude,
      phase: Math.random() * Math.PI * 2,
    })
  }
  return modes
}

function createPair(
  width: number,
  height: number,
  now: number,
  pairRate: number,
  spread: boolean,
): VirtualPair {
  const direction = Math.random() * Math.PI * 2
  const reach = PAIR_MIN_REACH + Math.random() * (PAIR_MAX_REACH - PAIR_MIN_REACH)
  const lifetime = (reach * PAIR_LIFETIME_PER_PX) / pairRate

  return {
    centerX: Math.random() * width,
    centerY: Math.random() * height,
    directionX: Math.cos(direction),
    directionY: Math.sin(direction),
    reach,
    lifetime,
    /* 初期化時だけ寿命の途中から始めて、全部が同時に光るのを避ける。 */
    bornAt: spread ? now - Math.random() * lifetime : now,
  }
}

type QuantumFluctuationFieldProps = {
  /* 今いる画面に対応する相。場の性格をこれで切り替える。 */
  regime: ModeId
  /* 下地としてどれだけ前に出るか。既定は控えめな ambient。 */
  presence?: FieldPresence
  /*
   * 描画に割く予算。'full' は 30fps、'reduced' は 20fps。濃さ（presence）とは
   * 別に持つ。作業画面は場を素の濃さで出しつつ、重いキャンバスと同居するので
   * フレームだけ落としたい。
   */
  motionBudget?: 'full' | 'reduced'
}

export function QuantumFluctuationField({
  regime,
  presence = 'ambient',
  motionBudget = 'reduced',
}: QuantumFluctuationFieldProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const { animationsEnabled } = useAnimationSettings()
  const { theme } = useTheme()
  const prefersReducedMotion = usePrefersReducedMotion()

  /*
   * 相と出し具合は ref で渡す。prop を effect の依存に入れると、画面を
   * 移るたびにキャンバスごと作り直されて模様がリセットされる。走っている
   * ループに値だけ差し込んで、あとは緩和で追わせたい。
   */
  const regimeRef = useRef(regime)
  const presenceRef = useRef(presence)
  const motionBudgetRef = useRef(motionBudget)
  /* アニメーションを切っている環境で、変更を1枚だけ焼き直すための口。 */
  const redrawRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    regimeRef.current = regime
    presenceRef.current = presence
    motionBudgetRef.current = motionBudget
    redrawRef.current?.()
  }, [regime, presence, motionBudget])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) {
      return
    }
    const context = canvas.getContext('2d')
    if (!context) {
      return
    }

    const buffer = document.createElement('canvas')
    const bufferContext = buffer.getContext('2d')
    if (!bufferContext) {
      return
    }

    const shouldAnimate = animationsEnabled && !prefersReducedMotion
    const palette = readPalette(theme === 'light')
    const modes = createModes()
    const amplitudes = new Float64Array(MODE_COUNT)

    /* 今の相。目標（regimeRef）へ向けて毎フレーム緩和させる。 */
    const current: FieldRegime = { ...REGIMES[regimeRef.current] }
    let presenceLevel = PRESENCE_LEVELS[presenceRef.current]

    let width = 0
    let height = 0
    let cols = 0
    let rows = 0
    let field = new Float32Array(0)
    let image: ImageData | null = null
    let cosAlongX = new Float32Array(0)
    let sinAlongX = new Float32Array(0)
    let cosAlongY = new Float32Array(0)
    let sinAlongY = new Float32Array(0)
    let pairs: VirtualPair[] = []

    const resize = () => {
      width = window.innerWidth
      height = window.innerHeight
      if (width === 0 || height === 0) {
        return
      }

      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.round(width * ratio)
      canvas.height = Math.round(height * ratio)
      context.setTransform(ratio, 0, 0, ratio, 0, 0)

      cols = GRID_COLS
      rows = Math.max(MIN_ROWS, Math.min(MAX_ROWS, Math.round((GRID_COLS * height) / width)))
      buffer.width = cols
      buffer.height = rows

      field = new Float32Array(cols * rows)
      image = bufferContext.createImageData(cols, rows)
      cosAlongX = new Float32Array(cols)
      sinAlongX = new Float32Array(cols)
      cosAlongY = new Float32Array(rows)
      sinAlongY = new Float32Array(rows)

      const now = performance.now() / 1000
      pairs = Array.from({ length: PAIR_COUNT }, () =>
        createPair(width, height, now, current.pairRate, true))
    }

    /*
     * 相を目標へ寄せ、位相を進める。緩和は指数（1 − e^{−dt/τ}）なので、
     * フレームレートが揺れても見た目の速さは変わらない。
     */
    const advance = (elapsed: number, snap: boolean) => {
      const target = REGIMES[regimeRef.current]
      const blend = snap ? 1 : 1 - Math.exp(-elapsed / PHASE_RELAXATION)

      current.mass += (target.mass - current.mass) * blend
      current.wavenumberScale += (target.wavenumberScale - current.wavenumberScale) * blend
      current.timeScale += (target.timeScale - current.timeScale) * blend
      current.pairRate += (target.pairRate - current.pairRate) * blend
      current.troughThreshold += (target.troughThreshold - current.troughThreshold) * blend
      current.intensity += (target.intensity - current.intensity) * blend
      presenceLevel += (PRESENCE_LEVELS[presenceRef.current] - presenceLevel) * blend

      /* ω と零点振幅は、そのときの質量と k の倍率から引き直す。 */
      let amplitudeTotal = 0
      for (let index = 0; index < modes.length; index += 1) {
        const mode = modes[index]
        const scaled = mode.magnitude * current.wavenumberScale
        const angularFrequency = Math.sqrt(scaled * scaled + current.mass * current.mass)
        mode.phase += angularFrequency * current.timeScale * elapsed
        amplitudes[index] = 1 / Math.sqrt(2 * angularFrequency)
        amplitudeTotal += amplitudes[index]
      }
      for (let index = 0; index < amplitudes.length; index += 1) {
        amplitudes[index] /= amplitudeTotal
      }
    }

    const drawField = () => {
      if (!image) {
        return
      }

      field.fill(0)

      /*
       * cos(kx·x + ky·y + β) を加法定理で
       *   cos(kx·x + β)·cos(ky·y) − sin(kx·x + β)·sin(ky·y)
       * に分解する。三角関数は行と列で1回ずつ引けば足りる。
       */
      const step = current.wavenumberScale / cols
      for (let index = 0; index < modes.length; index += 1) {
        const mode = modes[index]
        const amplitude = amplitudes[index]

        for (let x = 0; x < cols; x += 1) {
          const argument = mode.waveNumberX * (x * step) - mode.phase
          cosAlongX[x] = Math.cos(argument)
          sinAlongX[x] = Math.sin(argument)
        }
        for (let y = 0; y < rows; y += 1) {
          const argument = mode.waveNumberY * (y * step)
          cosAlongY[y] = Math.cos(argument)
          sinAlongY[y] = Math.sin(argument)
        }

        for (let y = 0; y < rows; y += 1) {
          const rowCos = cosAlongY[y] * amplitude
          const rowSin = sinAlongY[y] * amplitude
          const rowStart = y * cols
          for (let x = 0; x < cols; x += 1) {
            field[rowStart + x] += cosAlongX[x] * rowCos - sinAlongX[x] * rowSin
          }
        }
      }

      /* 実効的な最大振幅は総和より小さいので、少し持ち上げてから飽和させる。 */
      const gain = 1.85
      const alpha = palette.fieldAlpha * current.intensity * presenceLevel
      const threshold = current.troughThreshold
      const data = image.data
      const [inkR, inkG, inkB] = palette.ink
      const [accentR, accentG, accentB] = palette.accent

      for (let index = 0; index < field.length; index += 1) {
        const value = Math.max(-1, Math.min(1, field[index] * gain))
        const magnitude = Math.abs(value)
        const level = Math.round(magnitude * POSTERIZE_LEVELS) / POSTERIZE_LEVELS
        const isDeepTrough = value < -threshold

        const target = index * 4
        data[target] = isDeepTrough ? accentR : inkR
        data[target + 1] = isDeepTrough ? accentG : inkG
        data[target + 2] = isDeepTrough ? accentB : inkB
        data[target + 3] = Math.round(level * alpha * 255)
      }

      bufferContext.putImageData(image, 0, 0)
      context.clearRect(0, 0, width, height)
      context.imageSmoothingEnabled = true
      context.imageSmoothingQuality = 'high'
      context.drawImage(buffer, 0, 0, width, height)
    }

    const drawPairs = (seconds: number) => {
      const [inkR, inkG, inkB] = palette.ink
      const [accentR, accentG, accentB] = palette.accent

      for (let index = 0; index < pairs.length; index += 1) {
        const pair = pairs[index]
        const age = (seconds - pair.bornAt) / pair.lifetime

        if (age >= 1) {
          pairs[index] = createPair(width, height, seconds, current.pairRate, false)
          continue
        }
        if (age < 0) {
          continue
        }

        /*
         * 生成点から離れ、また戻って対消滅する。sin の半周期そのまま。
         * 明るさも同じ包絡で、寿命の真ん中がいちばん濃い。
         */
        const envelope = Math.sin(Math.PI * age)
        const separation = envelope * pair.reach * 0.5
        /*
         * 対だけは presence を二乗で効かせる。輪郭のはっきりした点は
         * 滲んだ場より目を引くので、引くときは場より深く引く。
         */
        const alpha = envelope ** 0.7 * palette.pairAlpha * current.intensity * presenceLevel ** 2

        const particleX = pair.centerX + pair.directionX * separation
        const particleY = pair.centerY + pair.directionY * separation
        const antiparticleX = pair.centerX - pair.directionX * separation
        const antiparticleY = pair.centerY - pair.directionY * separation

        context.strokeStyle = `rgb(${inkR} ${inkG} ${inkB} / ${alpha * 0.28})`
        context.lineWidth = 1
        context.beginPath()
        context.moveTo(particleX, particleY)
        context.lineTo(antiparticleX, antiparticleY)
        context.stroke()

        /* 粒子は塗り、反粒子は抜き。荷電共役を塗り分けで示す。 */
        context.fillStyle = `rgb(${inkR} ${inkG} ${inkB} / ${alpha})`
        context.fillRect(Math.round(particleX) - 1.5, Math.round(particleY) - 1.5, 3, 3)

        context.strokeStyle = `rgb(${accentR} ${accentG} ${accentB} / ${alpha})`
        context.strokeRect(Math.round(antiparticleX) - 2, Math.round(antiparticleY) - 2, 4, 4)
      }
    }

    let frame = 0
    let lastFrameAt = 0

    const render = (timestamp: number) => {
      frame = window.requestAnimationFrame(render)

      const interval = motionBudgetRef.current === 'full'
        ? FEATURE_FRAME_INTERVAL_MS
        : AMBIENT_FRAME_INTERVAL_MS
      if (timestamp - lastFrameAt < interval) {
        return
      }

      const elapsed = lastFrameAt === 0
        ? 0
        : Math.min((timestamp - lastFrameAt) / 1000, MAX_TIME_STEP)
      lastFrameAt = timestamp

      advance(elapsed, false)
      drawField()
      drawPairs(timestamp / 1000)
    }

    /* 動かさない環境向けの一枚焼き。相は緩和させず、いきなり目標へ置く。 */
    const paint = () => {
      advance(0, true)
      drawField()
      drawPairs(performance.now() / 1000)
    }

    resize()
    if (shouldAnimate) {
      frame = window.requestAnimationFrame(render)
    } else {
      redrawRef.current = paint
      paint()
    }

    const handleResize = () => {
      resize()
      if (!shouldAnimate) {
        paint()
      }
    }

    window.addEventListener('resize', handleResize)
    return () => {
      window.removeEventListener('resize', handleResize)
      redrawRef.current = null
      if (frame !== 0) {
        window.cancelAnimationFrame(frame)
      }
    }
  }, [animationsEnabled, prefersReducedMotion, theme])

  return <canvas className="quantum-fluctuation-field" ref={canvasRef} aria-hidden="true" />
}
