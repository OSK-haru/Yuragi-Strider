import { createRequire } from 'node:module'
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import ts from 'typescript'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/*
 * ソース断片の一致を見るアサーションが並ぶので、改行コードをLFに正規化して
 * から比較する。Windowsのチェックアウトや git の autocrlf でCRLFになると、
 * コードが正しくてもリテラル一致が外れて偽陽性で落ちるため。
 */
function readSource(relativePath) {
  return readFileSync(path.join(root, relativePath), 'utf8').replace(/\r\n/g, '\n')
}

const pulseLabPageSource = readSource('src/pages/PulseLabPage.tsx')
assert(
  !pulseLabPageSource.includes('onOpenCircuitStudio'),
  'Pulse Lab must not link to the gate-aware Circuit Studio',
)
assert(
  !pulseLabPageSource.includes('onOpenStateExplorer'),
  'Pulse Lab must not link to the gate-aware State Explorer',
)
/* Pulse IDE: the work area switches to the Pulse-level State Explorer in place. */
assert(
  pulseLabPageSource.includes("setIdeView('explorer')") && pulseLabPageSource.includes('ide.renderExplorer('),
  'Pulse IDE must open the Pulse-level State Explorer',
)
assert(
  pulseLabPageSource.includes('複数レーン同時実行。'),
  'Pulse Lab scope boundary is not visible',
)
assert(
  !pulseLabPageSource.includes('globalForm.modelId !== QUTRIT_PULSE_MODEL || sequence.length === 0'),
  'an empty qutrit circuit must not fall back to a single pulse',
)
assert(
  pulseLabPageSource.includes('if (sequence.length === 0) {\n    return []'),
  'an empty qutrit circuit must have an empty execution plan',
)

const pulseStateExplorerSource = readSource('src/pages/PulseStateExplorerPage.tsx')
assert(
  !pulseStateExplorerSource.includes('BlochSphere'),
  'Pulse State Explorer must not render a reduced Bloch sphere',
)
for (const gateAwareModule of [
  'components/BlochSphereExplorer',
  'components/DensityMatrixViewer',
  'components/MetricTimeline',
  'components/PhysicalTimelinePlayback',
  'components/StateProbabilityComparison',
  'context/useCircuitContext',
  'types/simulation',
]) {
  assert(
    !pulseStateExplorerSource.includes(gateAwareModule),
    `Pulse State Explorer must not depend on the gate-aware module ${gateAwareModule}`,
  )
}

const temporaryRoot = mkdtempSync(path.join(tmpdir(), 'yuragi-strider-pulse-lab-'))
const temporarySource = path.join(temporaryRoot, 'src')
mkdirSync(path.join(temporarySource, 'utils'), { recursive: true })
mkdirSync(path.join(temporarySource, 'types'), { recursive: true })
writeFileSync(path.join(temporaryRoot, 'package.json'), '{"type":"commonjs"}')

for (const relativePath of [
  'src/types/pulse.ts',
  'src/types/pulseCircuit.ts',
  'src/utils/pulseDeviceProfiles.ts',
  'src/utils/pulseCircuit.ts',
  'src/utils/pulseLab.ts',
  'src/utils/pulseStateExplorer.ts',
]) {
  const sourcePath = path.join(root, relativePath)
  const outputPath = path.join(
    temporaryRoot,
    relativePath.replace(/\.ts$/, '.js'),
  )
  const output = ts.transpileModule(readFileSync(sourcePath, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: sourcePath,
  }).outputText
  writeFileSync(outputPath, output)
}

const require = createRequire(import.meta.url)
const {
  buildPulsePayload,
  buildTransmonNetworkPayload,
  deriveModelId,
  estimatePulseCost,
  initialPulseLabForm,
  pulseWaveform,
  sequentialPulseWaveform,
  circuitLaneWaveform,
  validatePulseLabForm,
} = require(path.join(temporarySource, 'utils', 'pulseLab.js'))
const {
  createDefaultPulseCircuit,
  resizePulseCircuit,
} = require(path.join(temporarySource, 'utils', 'pulseCircuit.js'))

const qutritPayload = buildPulsePayload(initialPulseLabForm)
assert(
  qutritPayload.evolution_method === 'fixed_step_rk4',
  'Pulse Lab must preserve the RK4 API default',
)
assert(qutritPayload.anharmonicity_mhz === -100, 'qutrit anharmonicity missing')
assert(qutritPayload.pulse.drag_beta_us === 0.001, 'qutrit DRAG missing')
assert(!('gamma_down_per_us' in qutritPayload.environment), 'inactive two-level rate leaked')
assert(!('readout_error' in qutritPayload), 'disabled readout error leaked into payload')

const readoutPayload = buildPulsePayload({
  ...initialPulseLabForm,
  readoutErrorEnabled: true,
  readoutP10: 0.005,
  readoutP01: 0.013,
})
assert(readoutPayload.readout_error.p10 === 0.005, 'readout p10 missing')
assert(readoutPayload.readout_error.p01 === 0.013, 'readout p01 missing')
assert(
  Object.keys(validatePulseLabForm({
    ...initialPulseLabForm,
    readoutErrorEnabled: true,
    readoutP10: 0.6,
    readoutP01: 0.4,
  })).length > 0,
  'non-positive readout assignment span was not rejected',
)

const twoLevelForm = {
  ...initialPulseLabForm,
  modelId: 'driven_two_level_rwa_experimental_v1',
  shape: 'square',
  environmentMode: 'direct_rates',
}
const twoLevelPayload = buildPulsePayload(twoLevelForm)
assert(!('anharmonicity_mhz' in twoLevelPayload), 'qutrit field leaked into two-level payload')
assert(twoLevelPayload.pulse.drag_beta_us === 0, 'two-level DRAG must be zero')
assert(!('sigma_us' in twoLevelPayload.pulse), 'inactive Gaussian field leaked')
assert('gamma_down_per_us' in twoLevelPayload.environment, 'two-level direct rates missing')
assert(!('gamma_10_down_per_us' in twoLevelPayload.environment), 'qutrit rate leaked')

/*
 * 直接レート指定のとき、環境パネルが出す入力欄とペイロードが送るキーは
 * 「準位数 x 台数」から同じ結論を出さなければならない。かつて環境パネル側が
 * isTransmon = true でハードコードされていたため、2準位x1台では画面に
 * qutrit の5レートが出るのにペイロードは down/up/phi を送っており、
 * 編集した値がどこにも届かないまま初期値が飛んでいた。
 *
 * 画面の分岐条件（PulseEnvironmentPanel の usesTwoLevelRates）と
 * ペイロードの分岐（buildPulsePayload の modelId 判定）を、
 * deriveModelId 経由で突き合わせて固定する。
 */
const environmentPanelSource = readSource('src/components/PulseEnvironmentPanel.tsx')
assert(
  environmentPanelSource.includes(
    "const usesTwoLevelRates = form.localLevels === 2 && transmonCount === 1",
  ),
  'the environment panel must pick its rate fields from levels x transmon count',
)
assert(
  !/const isTransmon = true/.test(environmentPanelSource),
  'the environment panel must not hardcode the rate-field branch',
)

const TWO_LEVEL_RATE_KEYS = ['gamma_down_per_us', 'gamma_up_per_us', 'gamma_phi_per_us']
const QUTRIT_RATE_KEYS = [
  'gamma_10_down_per_us',
  'gamma_01_up_per_us',
  'gamma_21_down_per_us',
  'gamma_12_up_per_us',
  'gamma_phi_adjacent_per_us',
]

for (const localLevels of [2, 3]) {
  for (const transmonCount of [1, 2, 3, 4]) {
    /* 画面が3レート欄を出す条件。PulseEnvironmentPanel と同じ式を書き下す。 */
    const panelShowsTwoLevelRates = localLevels === 2 && transmonCount === 1
    const modelId = deriveModelId(localLevels, transmonCount)
    const form = {
      ...initialPulseLabForm,
      modelId,
      localLevels,
      environmentMode: 'direct_rates',
      /* 2準位に漏れ先はないので、画面側と同じく DRAG を落としておく。 */
      dragBetaUs: localLevels === 2 ? 0 : initialPulseLabForm.dragBetaUs,
    }
    const environment =
      transmonCount >= 2
        ? buildTransmonNetworkPayload(
            form,
            resizePulseCircuit(createDefaultPulseCircuit(form), transmonCount),
          ).environment
        : buildPulsePayload(form).environment

    const expected = panelShowsTwoLevelRates ? TWO_LEVEL_RATE_KEYS : QUTRIT_RATE_KEYS
    const forbidden = panelShowsTwoLevelRates ? QUTRIT_RATE_KEYS : TWO_LEVEL_RATE_KEYS
    const where = `L=${localLevels}, N=${transmonCount}`

    for (const key of expected) {
      assert(
        key in environment,
        `${where}: the panel shows ${key} but the payload does not send it`,
      )
    }
    for (const key of forbidden) {
      assert(
        !(key in environment),
        `${where}: the payload sends ${key} but the panel never shows it`,
      )
    }

    /*
     * 送られるキーは、画面で編集した値をそのまま運ばなければならない。
     * 初期値と違う値を入れて、素通りしていないことを確かめる。
     */
    const probe = { ...form }
    const fieldForKey = {
      gamma_down_per_us: 'gammaDownPerUs',
      gamma_up_per_us: 'gammaUpPerUs',
      gamma_phi_per_us: 'gammaPhiPerUs',
      gamma_10_down_per_us: 'gamma10DownPerUs',
      gamma_01_up_per_us: 'gamma01UpPerUs',
      gamma_21_down_per_us: 'gamma21DownPerUs',
      gamma_12_up_per_us: 'gamma12UpPerUs',
      gamma_phi_adjacent_per_us: 'gammaPhiAdjacentPerUs',
    }
    expected.forEach((key, index) => {
      probe[fieldForKey[key]] = 0.5 + index
    })
    const probed =
      transmonCount >= 2
        ? buildTransmonNetworkPayload(
            probe,
            resizePulseCircuit(createDefaultPulseCircuit(probe), transmonCount),
          ).environment
        : buildPulsePayload(probe).environment
    expected.forEach((key, index) => {
      assert(
        probed[key] === 0.5 + index,
        `${where}: edited ${key} did not reach the payload`,
      )
    })

    /* 編集した欄が検証されずに素通りしないこと（負値は弾かれる）。 */
    const invalid = { ...form }
    invalid[fieldForKey[expected[0]]] = -1
    assert(
      Object.keys(validatePulseLabForm(invalid)).length > 0,
      `${where}: a negative ${expected[0]} was not rejected`,
    )
  }
}

const networkForm = {
  ...initialPulseLabForm,
  modelId: 'driven_coupled_transmon_network_rwa_experimental_v1',
  totalSimulationTimeUs: 0.05,
}
const networkCircuit = resizePulseCircuit(
  createDefaultPulseCircuit(networkForm),
  3,
)
const networkPayload = buildTransmonNetworkPayload(networkForm, networkCircuit)
assert(networkPayload.transmon_count === 3, 'network transmon count missing')
assert(networkPayload.drives.length === 2, 'network lane schedule missing')
assert(networkPayload.couplings.length === 2, 'network nearest-neighbor couplings missing')
assert(networkPayload.evolution_method === 'fixed_step_rk4', 'network must use RK4')
const networkReadoutPayload = buildTransmonNetworkPayload(
  { ...networkForm, readoutErrorEnabled: true, readoutP10: 0.01, readoutP01: 0.02 },
  networkCircuit,
)
assert(networkReadoutPayload.readout_error.p10 === 0.01, 'network readout p10 missing')
assert(networkReadoutPayload.readout_error.p01 === 0.02, 'network readout p01 missing')
const calibratedNetworkPayload = buildTransmonNetworkPayload(
  {
    ...networkForm,
    readoutErrorEnabled: true,
    networkPerQubitCalibrationEnabled: true,
    networkT1MaxUs: [50, 75, 125, 100],
    networkTphiMaxUs: [60, 90, 140, 100],
    networkReadoutP10: [0.01, 0.02, 0.03, 0.04],
    networkReadoutP01: [0.04, 0.03, 0.02, 0.01],
  },
  networkCircuit,
)
assert(calibratedNetworkPayload.environment.per_qubit.length === 3, 'network coherence calibration count mismatch')
assert(calibratedNetworkPayload.environment.per_qubit[1].t1_max_us === 75, 'network q1 T1 calibration missing')
assert(calibratedNetworkPayload.readout_error.per_qubit.length === 3, 'network readout calibration count mismatch')
assert(calibratedNetworkPayload.readout_error.per_qubit[2].p10 === 0.03, 'network q2 readout calibration missing')

const cptpPayload = buildPulsePayload({
  ...initialPulseLabForm,
  evolutionMethod: 'explicit_cptp',
})
assert(
  cptpPayload.evolution_method === 'explicit_cptp',
  'explicit CPTP selection was not emitted',
)

const costly = estimatePulseCost({
  ...initialPulseLabForm,
  anharmonicityMhz: -250,
  sigmaUs: 0.02,
  totalSimulationTimeUs: 0.5,
})
assert(costly.overBudget, 'known costly qutrit request was not blocked')

const dragWaveform = pulseWaveform(initialPulseLabForm)
assert(
  dragWaveform.some((point) => Math.abs(point.omegaY) > 1e-9),
  'nonzero DRAG did not produce a visible Y quadrature',
)
const zeroDragWaveform = pulseWaveform({ ...initialPulseLabForm, dragBetaUs: 0 })
assert(
  zeroDragWaveform.every((point) => Math.abs(point.omegaY) < 1e-9),
  'zero-phase, zero-DRAG waveform unexpectedly contains Y quadrature',
)

const sequentialWaveform = sequentialPulseWaveform([
  { ...twoLevelForm, pulseDurationUs: 0.01, phaseRad: 0 },
  { ...twoLevelForm, pulseDurationUs: 0.01, phaseRad: Math.PI / 2 },
], 0.005, 5)
assert(
  sequentialWaveform.some((point) => point.timeUs === 0.01 && point.omegaX === 0 && point.omegaY === 0),
  'sequence waveform must return to zero at the start of the idle gap',
)
assert(
  sequentialWaveform.some((point) => point.timeUs === 0.015 && Math.abs(point.omegaY) > 1e-9),
  'second sequence pulse is missing from the scheduled waveform',
)
const emptyCircuitWaveform = sequentialPulseWaveform([], 0.005, 5)
assert(
  emptyCircuitWaveform.every((point) => point.omegaX === 0 && point.omegaY === 0),
  'an empty circuit must render a flat zero waveform',
)

const circuitWaveform = circuitLaneWaveform(networkForm, networkCircuit, 0, 5)
assert(
  circuitWaveform.some((point) => point.timeUs > 0.02),
  'circuit waveform must include every drive on the selected lane',
)

const {
  buildPulseExplorerView,
  nearestPulsePointIndex,
  pulseSetupSignature,
} = require(path.join(temporarySource, 'utils', 'pulseStateExplorer.js'))

const twoLevelView = buildPulseExplorerView(
  twoLevelExplorerResponse(),
  twoLevelForm,
)
assert(twoLevelView.dimension === 2, 'two-level explorer dimension is wrong')
assert(
  twoLevelView.points[1].idealPopulations['1'] === 1,
  'two-level explorer lost the closed-system reference series',
)
assert(
  twoLevelView.hasPerPointDensityMatrix === false,
  'two-level trajectory does not carry per-sample density matrices',
)
assert(twoLevelView.leakageLabel === null, 'two-level model cannot leak out of the qubit space')

const qutritView = buildPulseExplorerView(qutritExplorerResponse(), initialPulseLabForm)
assert(qutritView.dimension === 3, 'qutrit explorer dimension is wrong')
assert(
  qutritView.computationalLabels.join(',') === '0,1',
  'qutrit computational subspace must exclude the leakage level',
)
assert(
  qutritView.points[1].leakage === 0.25,
  'qutrit explorer lost the leakage series',
)
assert(qutritView.hasPerPointDensityMatrix, 'qutrit trajectory carries density matrices')
assert(
  nearestPulsePointIndex(qutritView.points, 0.019) === 1,
  'cursor did not snap to the nearest trajectory sample',
)

const qutritReadoutView = buildPulseExplorerView(
  {
    ...qutritExplorerResponse(),
    readout: {
      enabled: true,
      model_id: 'independent_affine_binary_readout_v1',
      assignment_errors: [{ p10: 0.1, p01: 0.1 }],
      true_probabilities: { '0': 0.5, '1': 0.25, '2': 0.25 },
      observed_probabilities: { '0': 0.5, '1': 0.25, '2': 0.25 },
      preserves_state_metrics: true,
      noncomputational_level_policy: 'preserve',
    },
  },
  initialPulseLabForm,
)
assert(qutritReadoutView.readoutEnabled, 'Pulse explorer did not enable readout display')
assert(
  qutritReadoutView.points[1].observedPopulations['2'] === 0.25,
  'qutrit leakage must be preserved by binary readout error',
)
assert(
  Math.abs(qutritReadoutView.points[1].observedPopulations['0'] - 0.475) < 1e-12,
  'Pulse explorer did not apply binary readout error to the selected point',
)

const baseSignature = pulseSetupSignature(initialPulseLabForm, networkCircuit)
assert(
  baseSignature === pulseSetupSignature(initialPulseLabForm, networkCircuit),
  'run signature must be stable for an unchanged setup',
)
assert(
  baseSignature !== pulseSetupSignature(
    { ...initialPulseLabForm, temperatureMk: initialPulseLabForm.temperatureMk + 1 },
    networkCircuit,
  ),
  'run signature must change when the environment changes',
)

console.log('Pulse Lab payload and waveform contract: PASS')
console.log('Pulse State Explorer trajectory normalisation: PASS')
rmSync(temporaryRoot, { recursive: true, force: true })

function assert(condition, message) {
  if (!condition) {
    throw new Error(message)
  }
}

/* 状態エクスプローラーの正規化だけを見たいので、軌跡は最小限の2点で作る。 */
function twoLevelExplorerResponse() {
  const point = (timeUs, open1) => ({
    time_us: timeUs,
    segment: timeUs === 0 ? 'pulse' : 'idle',
    open_population_0: 1 - open1,
    open_population_1: open1,
    closed_population_0: 1 - Math.round(open1),
    closed_population_1: Math.round(open1),
    fidelity_to_closed: 0.99,
    purity: 0.98,
  })
  const state = { ...point(0.02, 0.9), open_density_matrix: [[{ real: 0.1, imag: 0 }, { real: 0, imag: 0 }], [{ real: 0, imag: 0 }, { real: 0.9, imag: 0 }]] }
  return {
    contract_version: 'pulse-baseline-a-v1',
    model: { model_id: 'driven_two_level_rwa_experimental_v1' },
    sample_times_us: [0, 0.02],
    trajectory: [point(0, 0), point(0.02, 0.9)],
    pulse_end: state,
    final: state,
    warnings: [],
    limitations: [],
  }
}

function qutritExplorerResponse() {
  const matrix = [
    [{ real: 0.5, imag: 0 }, { real: 0, imag: 0 }, { real: 0, imag: 0 }],
    [{ real: 0, imag: 0 }, { real: 0.25, imag: 0 }, { real: 0, imag: 0 }],
    [{ real: 0, imag: 0 }, { real: 0, imag: 0 }, { real: 0.25, imag: 0 }],
  ]
  const point = (timeUs, leakage) => ({
    time_us: timeUs,
    segment: 'pulse',
    population_0: 0.5,
    population_1: 0.5 - leakage,
    population_2: leakage,
    computational_population: 1 - leakage,
    leakage_probability: leakage,
    population_sum_error: 0,
    purity: 0.9,
    density_matrix: matrix,
  })
  return {
    contract_version: 'pulse-extension-b-v1',
    model: { model_id: 'driven_transmon_qutrit_rwa_experimental_v1', basis_order: ['0', '1', '2'] },
    input: {},
    sample_times_us: [0, 0.02],
    trajectory: [point(0, 0), point(0.02, 0.25)],
    leakage: {
      maximum_recorded_leakage_probability: 0.25,
      leakage_at_pulse_end: 0.25,
      leakage_at_final_time: 0.25,
    },
    pulse_end: point(0.02, 0.25),
    final: point(0.02, 0.25),
    warnings: [],
    limitations: [],
  }
}
