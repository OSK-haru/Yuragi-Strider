import {
  COUPLED_TRANSMON_NETWORK_PULSE_MODEL,
  QUTRIT_PULSE_MODEL,
  TWO_LEVEL_PULSE_MODEL,
  type PulseCostEstimate,
  type PulseLabErrors,
  type PulseLabForm,
  type PulseComplexValue,
  type PulseResponse,
  type PulseWaveformPoint,
  type QutritPulsePoint,
} from '../types/pulse'
import type { PulseCircuitState } from '../types/pulseCircuit'
import {
  applyPulseStepToForm,
  isDrivePulseStep,
  normalizeFramePhase,
  pulseStepDurationUs,
} from './pulseCircuit'

const QUTRIT_API_MAX_STEPS = 25000
const TWO_LEVEL_API_MAX_STEPS = 200000
const EPSILON_H = 0.02
const EPSILON_D = 0.02
const QUTRIT_SAMPLES_PER_SIGMA = 32
const MHZ_TO_RAD_PER_US = 2 * Math.PI
/*
 * api/pulse_transmon_network_service.py と同じ予算。1ステップの費用は
 * 固定オーバーヘッドと dim^3 の密行列演算の和として数える。
 */
const NETWORK_STEP_OVERHEAD_UNITS = 12000
const NETWORK_MAX_DENSE_WORK_UNITS = 1200000000
const NETWORK_MAX_RESPONSE_MATRIX_ELEMENTS = 250000
const NETWORK_CPTP_MAX_INTERVALS = 500
const NETWORK_CPTP_STEP_RELAXATION = 3

export const initialPulseLabForm: PulseLabForm = {
  modelId: QUTRIT_PULSE_MODEL,
  localLevels: 3,
  evolutionMethod: 'fixed_step_rk4',
  backend: 'auto',
  shape: 'gaussian',
  amplitudeMode: 'target_rotation_angle',
  targetRotationAngleRad: Math.PI / 2,
  peakAmplitudeRadPerUs: 20,
  pulseDurationUs: 0.02,
  sigmaUs: 0.002,
  truncationSigma: 4,
  totalSimulationTimeUs: 0.02,
  phaseRad: 0,
  detuningRadPerUs: 0,
  anharmonicityMhz: -100,
  networkDetuningQ0RadPerUs: 0,
  networkDetuningQ1RadPerUs: 30,
  networkExchangeCouplingRadPerUs: 5,
  dragBetaUs: 0.001,
  environmentMode: 'physical',
  deviceQuality: 0.8,
  temperatureMk: 15,
  fluxNoisePhi0: 0.000001,
  qubitFrequencyGhz: 5,
  t1MaxUs: 100,
  tphiMaxUs: 100,
  gammaDownPerUs: 0.1,
  gammaUpPerUs: 0.02,
  gammaPhiPerUs: 0.05,
  gamma10DownPerUs: 0.2,
  gamma01UpPerUs: 0.02,
  gamma21DownPerUs: 0.4,
  gamma12UpPerUs: 0.03,
  gammaPhiAdjacentPerUs: 0.08,
  quasiStaticNoiseEnabled: false,
  quasiStaticDetuningSigmaRadPerUs: 0.5,
  quasiStaticQuadratureOrder: 5,
  networkQuasiStaticSigmaRadPerUs: 0,
  networkQuasiStaticAdjacentCorrelation: 0,
  networkQuasiStaticQuadratureOrder: 3,
  networkPerQubitCalibrationEnabled: false,
  networkT1MaxUs: [100, 100, 100, 100],
  networkTphiMaxUs: [100, 100, 100, 100],
  networkReadoutP10: [0.005, 0.005, 0.005, 0.005],
  networkReadoutP01: [0.013, 0.013, 0.013, 0.013],
  readoutErrorEnabled: false,
  readoutP10: 0.005,
  readoutP01: 0.013,
  snapshotCount: 101,
}

export function pulseDurationUs(form: PulseLabForm): number {
  return form.shape === 'square'
    ? form.pulseDurationUs
    : 2 * form.sigmaUs * form.truncationSigma
}

/*
 * 「準位数 × 台数」を旧 modelId に写す。1台は単一Pulseの専用モデル、
 * 2台以上は準位数を local_levels として渡すネットワークモデル。
 */
export function deriveModelId(
  localLevels: 2 | 3,
  transmonCount: number,
): PulseLabForm['modelId'] {
  if (transmonCount >= 2) {
    return COUPLED_TRANSMON_NETWORK_PULSE_MODEL
  }
  return localLevels === 2 ? TWO_LEVEL_PULSE_MODEL : QUTRIT_PULSE_MODEL
}

export function validatePulseLabForm(form: PulseLabForm, transmonCount = 4): PulseLabErrors {
  const errors: PulseLabErrors = {}
  const positive = (key: keyof PulseLabForm, value: number, label: string) => {
    if (!Number.isFinite(value) || value <= 0) {
      errors[key] = `${label}は0より大きい値である必要があります。`
    }
  }
  const nonnegative = (key: keyof PulseLabForm, value: number, label: string) => {
    if (!Number.isFinite(value) || value < 0) {
      errors[key] = `${label}は0以上である必要があります。`
    }
  }

  positive('totalSimulationTimeUs', form.totalSimulationTimeUs, '総シミュレーション時間')
  if (form.shape === 'square') {
    positive('pulseDurationUs', form.pulseDurationUs, 'Pulse幅')
  } else {
    positive('sigmaUs', form.sigmaUs, 'Gaussian σ')
    positive('truncationSigma', form.truncationSigma, '打ち切りσ')
  }
  if (form.amplitudeMode === 'target_rotation_angle') {
    if (!Number.isFinite(form.targetRotationAngleRad)) {
      errors.targetRotationAngleRad = '目標角度は有限の値である必要があります。'
    }
  } else {
    if (!Number.isFinite(form.peakAmplitudeRadPerUs)) {
      errors.peakAmplitudeRadPerUs = 'ピーク振幅は有限の値である必要があります。'
    }
  }
  if (!Number.isFinite(form.phaseRad)) {
    errors.phaseRad = '位相は有限の値である必要があります。'
  }
  if (!Number.isFinite(form.detuningRadPerUs)) {
    errors.detuningRadPerUs = 'デチューニングは有限の値である必要があります。'
  }
  nonnegative(
    'quasiStaticDetuningSigmaRadPerUs',
    form.quasiStaticDetuningSigmaRadPerUs,
    '準静的デチューニングσ',
  )
  if (form.quasiStaticNoiseEnabled && form.quasiStaticDetuningSigmaRadPerUs <= 0) {
    errors.quasiStaticDetuningSigmaRadPerUs =
      '準静的ノイズを有効にする場合、σは0より大きい値である必要があります。'
  }
  if (![3, 5, 7, 9].includes(form.quasiStaticQuadratureOrder)) {
    errors.quasiStaticQuadratureOrder = '直交次数は3、5、7、9のいずれかである必要があります。'
  }
  if (pulseDurationUs(form) > form.totalSimulationTimeUs) {
    errors.totalSimulationTimeUs =
      '総シミュレーション時間はPulse幅全体を含む必要があります。'
  }
  if (
    !Number.isInteger(form.snapshotCount) ||
    form.snapshotCount < 2 ||
    form.snapshotCount > 1001
  ) {
    errors.snapshotCount = 'スナップショット数は2から1001までの整数である必要があります。'
  }
  if (form.readoutErrorEnabled) {
    if (!Number.isFinite(form.readoutP10) || form.readoutP10 < 0 || form.readoutP10 > 1) {
      errors.readoutP10 = 'p10は0以上1以下の有限値にしてください。'
    }
    if (!Number.isFinite(form.readoutP01) || form.readoutP01 < 0 || form.readoutP01 > 1) {
      errors.readoutP01 = 'p01は0以上1以下の有限値にしてください。'
    }
    if (!errors.readoutP10 && !errors.readoutP01 && form.readoutP10 + form.readoutP01 >= 1) {
      errors.readoutP01 = 'p10 + p01は1未満にしてください。'
    }
  }

  if (form.modelId !== TWO_LEVEL_PULSE_MODEL) {
    if (!Number.isFinite(form.anharmonicityMhz) || form.anharmonicityMhz >= 0) {
      errors.anharmonicityMhz = '非調和性は有限の負の値である必要があります。'
    }
    if (form.shape === 'gaussian') {
      if (!Number.isFinite(form.dragBetaUs)) {
        errors.dragBetaUs = 'DRAG βは有限の値である必要があります。'
      }
    }
    if (
      form.environmentMode === 'physical' &&
      form.qubitFrequencyGhz + form.anharmonicityMhz / 1000 <= 0
    ) {
      errors.anharmonicityMhz =
        '結果として得られる|1>-|2>遷移周波数は正の値を保つ必要があります。'
    }
  }
  if (form.modelId === COUPLED_TRANSMON_NETWORK_PULSE_MODEL) {
    if (form.networkPerQubitCalibrationEnabled) {
      for (let index = 0; index < transmonCount; index += 1) {
        const t1 = form.networkT1MaxUs[index]
        const tphi = form.networkTphiMaxUs[index]
        const p10 = form.networkReadoutP10[index]
        const p01 = form.networkReadoutP01[index]
        if (!Number.isFinite(t1) || t1 <= 0) {
          errors.networkT1MaxUs = `q${index}の最大T1は0より大きい値にしてください。`
        }
        if (!Number.isFinite(tphi) || tphi <= 0) {
          errors.networkTphiMaxUs = `q${index}の最大Tφは0より大きい値にしてください。`
        }
        if (form.readoutErrorEnabled && (
          !Number.isFinite(p10) || !Number.isFinite(p01)
          || p10 < 0 || p10 > 1 || p01 < 0 || p01 > 1 || p10 + p01 >= 1
        )) {
          errors.networkReadoutP01 = `q${index}の読み出し誤差が有効範囲外です。`
        }
      }
    }
    if (!Number.isFinite(form.networkDetuningQ0RadPerUs)) {
      errors.networkDetuningQ0RadPerUs = 'q0の基準離調は有限値にしてください。'
    }
    if (!Number.isFinite(form.networkDetuningQ1RadPerUs)) {
      errors.networkDetuningQ1RadPerUs = 'q1の基準離調は有限値にしてください。'
    }
    nonnegative(
      'networkExchangeCouplingRadPerUs',
      form.networkExchangeCouplingRadPerUs,
      '隣接交換結合',
    )
    nonnegative(
      'networkQuasiStaticSigmaRadPerUs',
      form.networkQuasiStaticSigmaRadPerUs,
      '共通準静的σ',
    )
    if (
      !Number.isFinite(form.networkQuasiStaticAdjacentCorrelation) ||
      Math.abs(form.networkQuasiStaticAdjacentCorrelation) > 1
    ) {
      errors.networkQuasiStaticAdjacentCorrelation =
        '隣接相関係数は-1から1の間である必要があります。'
    }
    if (![3, 5].includes(form.networkQuasiStaticQuadratureOrder)) {
      errors.networkQuasiStaticQuadratureOrder = '求積次数は3または5です。'
    }
  }

  if (form.environmentMode === 'physical') {
    if (
      !Number.isFinite(form.deviceQuality) ||
      form.deviceQuality < 0 ||
      form.deviceQuality > 1
    ) {
      errors.deviceQuality = 'デバイス品質は0から1の間である必要があります。'
    }
    nonnegative('temperatureMk', form.temperatureMk, '温度')
    nonnegative('fluxNoisePhi0', form.fluxNoisePhi0, '磁束ノイズ')
    positive('qubitFrequencyGhz', form.qubitFrequencyGhz, '量子ビット周波数')
    positive('t1MaxUs', form.t1MaxUs, '最大T1')
    positive('tphiMaxUs', form.tphiMaxUs, '最大Tφ')
  } else if (form.modelId === TWO_LEVEL_PULSE_MODEL) {
    nonnegative('gammaDownPerUs', form.gammaDownPerUs, 'γ↓ (下降)')
    nonnegative('gammaUpPerUs', form.gammaUpPerUs, 'γ↑ (上昇)')
    nonnegative('gammaPhiPerUs', form.gammaPhiPerUs, 'γφ (位相緩和)')
  } else {
    nonnegative('gamma10DownPerUs', form.gamma10DownPerUs, 'γ10↓ (下降)')
    nonnegative('gamma01UpPerUs', form.gamma01UpPerUs, 'γ01↑ (上昇)')
    nonnegative('gamma21DownPerUs', form.gamma21DownPerUs, 'γ21↓ (下降)')
    nonnegative('gamma12UpPerUs', form.gamma12UpPerUs, 'γ12↑ (上昇)')
    nonnegative(
      'gammaPhiAdjacentPerUs',
      form.gammaPhiAdjacentPerUs,
      '隣接デフェージングレート',
    )
  }
  return errors
}

export function buildPulsePayload(
  form: PulseLabForm,
  initialDensityMatrix?: PulseComplexValue[][],
): Record<string, unknown> {
  const pulse =
    form.shape === 'square'
      ? {
          shape: 'square',
          amplitude_mode: form.amplitudeMode,
          ...(form.amplitudeMode === 'target_rotation_angle'
            ? { target_rotation_angle_rad: form.targetRotationAngleRad }
            : { peak_amplitude_rad_per_us: form.peakAmplitudeRadPerUs }),
          pulse_duration_us: form.pulseDurationUs,
          phase_rad: form.phaseRad,
          detuning_rad_per_us: form.detuningRadPerUs,
          drag_beta_us: 0,
        }
      : {
          shape: 'gaussian',
          amplitude_mode: form.amplitudeMode,
          ...(form.amplitudeMode === 'target_rotation_angle'
            ? { target_rotation_angle_rad: form.targetRotationAngleRad }
            : { peak_amplitude_rad_per_us: form.peakAmplitudeRadPerUs }),
          sigma_us: form.sigmaUs,
          truncation_sigma: form.truncationSigma,
          phase_rad: form.phaseRad,
          detuning_rad_per_us: form.detuningRadPerUs,
          drag_beta_us:
            form.modelId !== TWO_LEVEL_PULSE_MODEL ? form.dragBetaUs : 0,
        }
  const environment =
    form.environmentMode === 'physical'
      ? {
          input_mode: 'physical',
          device_quality: form.deviceQuality,
          temperature_mk: form.temperatureMk,
          flux_noise_phi0: form.fluxNoisePhi0,
          qubit_frequency_ghz: form.qubitFrequencyGhz,
          t1_max_us: form.t1MaxUs,
          tphi_max_us: form.tphiMaxUs,
        }
      : form.modelId === TWO_LEVEL_PULSE_MODEL
        ? {
            input_mode: 'direct_rates',
            gamma_down_per_us: form.gammaDownPerUs,
            gamma_up_per_us: form.gammaUpPerUs,
            gamma_phi_per_us: form.gammaPhiPerUs,
          }
        : {
            input_mode: 'direct_rates',
            gamma_10_down_per_us: form.gamma10DownPerUs,
            gamma_01_up_per_us: form.gamma01UpPerUs,
            gamma_21_down_per_us: form.gamma21DownPerUs,
            gamma_12_up_per_us: form.gamma12UpPerUs,
            gamma_phi_adjacent_per_us: form.gammaPhiAdjacentPerUs,
          }

  return {
    model_id: form.modelId,
    initial_state: '0',
    ...(initialDensityMatrix ? { initial_density_matrix: initialDensityMatrix } : {}),
    ...(form.modelId === QUTRIT_PULSE_MODEL
      ? {
          anharmonicity_mhz: form.anharmonicityMhz,
          quasi_static_noise: {
            enabled: form.quasiStaticNoiseEnabled,
            sigma_detuning_rad_per_us: form.quasiStaticNoiseEnabled
              ? form.quasiStaticDetuningSigmaRadPerUs
              : 0,
            quadrature_order: form.quasiStaticQuadratureOrder,
          },
        }
      : {}),
    pulse,
    total_simulation_time_us: form.totalSimulationTimeUs,
    evolution_method: form.evolutionMethod,
    backend: form.backend,
    environment,
    ...(form.readoutErrorEnabled ? {
      readout_error: { p10: form.readoutP10, p01: form.readoutP01 },
    } : {}),
    snapshot_options: {
      uniform_count: form.snapshotCount,
      custom_times_us: [pulseDurationUs(form)],
    },
  }
}

export function buildTransmonNetworkPayload(
  form: PulseLabForm,
  circuit: PulseCircuitState,
): Record<string, unknown> {
  const transmonCount = circuit.transmons.length
  const drives: Array<Record<string, unknown>> = []
  const boundaryTimes = new Set<number>()

  circuit.lanes.forEach((lane) => {
    let startTimeUs = 0
    let framePhaseRad = 0
    lane.steps.forEach((step, stepIndex) => {
      if (!isDrivePulseStep(step)) {
        framePhaseRad = normalizeFramePhase(framePhaseRad + step.angleRad)
        return
      }
      const pulseForm = applyPulseStepToForm(form, step.pulse)
      const durationUs = pulseStepDurationUs(step.pulse)
      drives.push({
        target: lane.transmonIndex,
        start_time_us: startTimeUs,
        pulse: {
          ...qutritPulsePayload(pulseForm),
          phase_rad: normalizeFramePhase(pulseForm.phaseRad + framePhaseRad),
        },
      })
      boundaryTimes.add(startTimeUs)
      boundaryTimes.add(startTimeUs + durationUs)
      const hasLaterDrive = lane.steps
        .slice(stepIndex + 1)
        .some(isDrivePulseStep)
      startTimeUs += durationUs + (hasLaterDrive
        ? circuit.executionConstraints.interPulseGapUs
        : 0)
    })
  })

  const dimension = form.localLevels ** transmonCount
  const boundaryReserve = Math.max(0, boundaryTimes.size - 2)
  const snapshotLimit = Math.max(
    2,
    Math.floor(NETWORK_MAX_RESPONSE_MATRIX_ELEMENTS / (dimension * dimension))
      - boundaryReserve,
  )
  const environment = form.environmentMode === 'physical'
    ? {
        input_mode: 'physical',
        device_quality: form.deviceQuality,
        temperature_mk: form.temperatureMk,
        flux_noise_phi0: form.fluxNoisePhi0,
        qubit_frequency_ghz: form.qubitFrequencyGhz,
        t1_max_us: form.t1MaxUs,
        tphi_max_us: form.tphiMaxUs,
        ...(form.networkPerQubitCalibrationEnabled ? {
          per_qubit: Array.from({ length: transmonCount }, (_, index) => ({
            t1_max_us: form.networkT1MaxUs[index] ?? form.t1MaxUs,
            tphi_max_us: form.networkTphiMaxUs[index] ?? form.tphiMaxUs,
          })),
        } : {}),
      }
    : {
        input_mode: 'direct_rates',
        gamma_10_down_per_us: form.gamma10DownPerUs,
        gamma_01_up_per_us: form.gamma01UpPerUs,
        gamma_21_down_per_us: form.gamma21DownPerUs,
        gamma_12_up_per_us: form.gamma12UpPerUs,
        gamma_phi_adjacent_per_us: form.gammaPhiAdjacentPerUs,
      }

  const networkSigma = Math.max(0, form.networkQuasiStaticSigmaRadPerUs)

  return {
    model_id: COUPLED_TRANSMON_NETWORK_PULSE_MODEL,
    transmon_count: transmonCount,
    local_levels: form.localLevels,
    initial_state: '0'.repeat(transmonCount),
    frequencies_ghz: circuit.transmons.map((transmon) => transmon.frequencyGhz),
    anharmonicities_mhz: circuit.transmons.map((transmon) => transmon.anharmonicityMhz),
    detunings_rad_per_us: Array.from(
      { length: transmonCount },
      (_, index) => index === 0
        ? form.networkDetuningQ0RadPerUs
        : index === 1
          ? form.networkDetuningQ1RadPerUs
          : 0,
    ),
    couplings: Array.from({ length: Math.max(0, transmonCount - 1) }, (_, left) => ({
      left,
      right: left + 1,
      exchange_coupling_rad_per_us: form.networkExchangeCouplingRadPerUs,
    })),
    drives,
    ...(networkSigma > 0
      ? {
          quasi_static_detuning_sigmas_rad_per_us: Array.from(
            { length: transmonCount },
            () => networkSigma,
          ),
          quasi_static_detuning_adjacent_correlation:
            form.networkQuasiStaticAdjacentCorrelation,
          quasi_static_quadrature_order: form.networkQuasiStaticQuadratureOrder,
        }
      : {}),
    total_simulation_time_us: form.totalSimulationTimeUs,
    evolution_method: form.localLevels ** transmonCount <= 9
      ? form.evolutionMethod
      : 'fixed_step_rk4',
    backend: form.backend,
    environment,
    ...(form.readoutErrorEnabled ? {
      readout_error: {
        p10: form.readoutP10,
        p01: form.readoutP01,
        ...(form.networkPerQubitCalibrationEnabled ? {
          per_qubit: Array.from({ length: transmonCount }, (_, index) => ({
            p10: form.networkReadoutP10[index] ?? form.readoutP10,
            p01: form.networkReadoutP01[index] ?? form.readoutP01,
          })),
        } : {}),
      },
    } : {}),
    snapshot_options: {
      uniform_count: Math.min(form.snapshotCount, snapshotLimit),
      custom_times_us: [...boundaryTimes].filter(
        (timeUs) => timeUs <= form.totalSimulationTimeUs,
      ),
    },
  }
}

export function estimateTransmonNetworkCost(
  form: PulseLabForm,
  circuit: PulseCircuitState,
): PulseCostEstimate {
  const driveForms = circuit.lanes.flatMap((lane) => lane.steps
    .filter(isDrivePulseStep)
    .map((step) => ({
      transmonIndex: lane.transmonIndex,
      form: applyPulseStepToForm(form, step.pulse),
    })))
  const dimension = form.localLevels ** circuit.transmons.length
  const effectiveSnapshotCount = Math.min(
    form.snapshotCount,
    Math.max(
      2,
      Math.floor(NETWORK_MAX_RESPONSE_MATRIX_ELEMENTS / (dimension * dimension))
        - 2 * driveForms.length,
    ),
  )
  /*
   * 刻み幅はAPIの step policy と同じ量で決める。ハミルトニアンの
   * 固有値スパンを上界で置き換えるとAPIが受け付ける要求までUIが
   * 止めてしまうため、ここでは3準位の固有値スパンを直接使う。
   */
  const anharmonicityRadPerUs = MHZ_TO_RAD_PER_US * Math.min(
    ...circuit.transmons.map((transmon) => transmon.anharmonicityMhz),
  )
  const couplingStepLimitUs = form.networkExchangeCouplingRadPerUs > 0
    ? EPSILON_H / (4 * Math.abs(form.networkExchangeCouplingRadPerUs))
    : Number.POSITIVE_INFINITY
  const stepCapUs = Math.min(
    couplingStepLimitUs,
    ...driveForms.map(({ transmonIndex, form: driveForm }) => qutritStepCapUs(
      driveForm,
      anharmonicityRadPerUs,
      Math.abs(networkBaseDetuningRadPerUs(form, transmonIndex))
        + Math.abs(driveForm.detuningRadPerUs),
    )),
  )
  /*
   * CPTP は各区間で中点凍結した指数写像を合成するため、RK4 の刻みを
   * NETWORK_CPTP_STEP_RELAXATION 倍まで緩められる。ただし区間数そのものが
   * NETWORK_CPTP_MAX_INTERVALS で頭打ちになるので、予算はステップ数ではなく
   * 区間数で評価する。ここを見ないと UI が「実行可能」と表示したまま
   * API が 422 を返す。
   */
  const isCptp = form.evolutionMethod === 'explicit_cptp'
  const integrationStepUs = isCptp
    ? Math.min(
        stepCapUs * NETWORK_CPTP_STEP_RELAXATION,
        /*
         * サーバは max(最終ドライブ終了時刻, 総時間)/8 を使うが、ドライブは
         * 総時間内に収まる制約があるので総時間が支配項になる。
         */
        form.totalSimulationTimeUs / 8,
        couplingStepLimitUs,
      )
    : stepCapUs
  const estimatedInternalSteps = driveForms.length === 0 || !(integrationStepUs > 0)
    ? effectiveSnapshotCount
    : Math.ceil(form.totalSimulationTimeUs / integrationStepUs) + effectiveSnapshotCount
  const maximumInternalSteps = isCptp
    ? NETWORK_CPTP_MAX_INTERVALS
    : Math.floor(
        NETWORK_MAX_DENSE_WORK_UNITS / (dimension ** 3 + NETWORK_STEP_OVERHEAD_UNITS),
      )
  return costResult(
    estimatedInternalSteps,
    maximumInternalSteps,
    isCptp ? 'cptp_intervals' : 'steps',
  )
}

export function networkBaseDetuningRadPerUs(
  form: PulseLabForm,
  transmonIndex: number,
): number {
  if (transmonIndex === 0) {
    return form.networkDetuningQ0RadPerUs
  }
  return transmonIndex === 1 ? form.networkDetuningQ1RadPerUs : 0
}

/*
 * `recommended_qutrit_step_policy` のTypeScript版。刻み幅を決める4つの
 * 上限（パルス長・固有値スパン・散逸・Gaussianの分解能）を同じ式で評価する。
 */
function qutritStepCapUs(
  form: PulseLabForm,
  anharmonicityRadPerUs: number,
  detuningRadPerUs: number,
): number {
  const duration = pulseDurationUs(form)
  const driveMagnitude = maximumDriveMagnitudeRadPerUs(form)
  const spectralDiameter = Math.max(
    qutritSpectralDiameterRadPerUs(detuningRadPerUs, anharmonicityRadPerUs, 0),
    qutritSpectralDiameterRadPerUs(
      detuningRadPerUs,
      anharmonicityRadPerUs,
      driveMagnitude,
    ),
  )
  const dissipationScale = form.environmentMode === 'direct_rates'
    ? form.gamma10DownPerUs
      + form.gamma01UpPerUs
      + form.gamma21DownPerUs
      + form.gamma12UpPerUs
      + 4 * form.gammaPhiAdjacentPerUs
    : 0
  const limits = [
    duration,
    ...(spectralDiameter > 0 ? [EPSILON_H / spectralDiameter] : []),
    ...(dissipationScale > 0 ? [EPSILON_D / dissipationScale] : []),
    ...(form.shape === 'gaussian' ? [form.sigmaUs / QUTRIT_SAMPLES_PER_SIGMA] : []),
  ]
  return Math.min(...limits.filter((value) => Number.isFinite(value) && value > 0))
}

function maximumDriveMagnitudeRadPerUs(form: PulseLabForm): number {
  const peak = Math.abs(peakAmplitude(form))
  if (form.shape !== 'gaussian' || form.dragBetaUs === 0) {
    return peak
  }
  /*
   * DRAGでは sqrt(Omega^2 + (beta dOmega/dt)^2) の最大値を取る。
   * 端点と、存在する場合の停留点を比べる。
   */
  const ratio = Math.abs(form.dragBetaUs) / form.sigmaUs
  const candidates = [0, form.truncationSigma]
  if (ratio > 1) {
    const stationary = Math.sqrt(1 - 1 / (ratio * ratio))
    if (stationary <= form.truncationSigma) {
      candidates.push(stationary)
    }
  }
  return Math.max(...candidates.map((normalized) => peak
    * Math.exp(-0.5 * normalized * normalized)
    * Math.sqrt(1 + (ratio * normalized) ** 2)))
}

/*
 * 3準位回転系ハミルトニアンの固有値スパン。実対称三重対角なので
 * 対称3x3の解析解をそのまま使える。
 */
function qutritSpectralDiameterRadPerUs(
  detuningRadPerUs: number,
  anharmonicityRadPerUs: number,
  amplitudeRadPerUs: number,
): number {
  const coupling01 = 0.5 * amplitudeRadPerUs
  const coupling12 = Math.SQRT2 * coupling01
  const diagonal = [
    0,
    -detuningRadPerUs,
    -2 * detuningRadPerUs + anharmonicityRadPerUs,
  ]
  const offDiagonalSquares = coupling01 ** 2 + coupling12 ** 2
  const mean = (diagonal[0] + diagonal[1] + diagonal[2]) / 3
  const shifted = diagonal.map((value) => value - mean)
  const scale = Math.sqrt(
    (shifted[0] ** 2 + shifted[1] ** 2 + shifted[2] ** 2 + 2 * offDiagonalSquares) / 6,
  )
  if (scale === 0) {
    return 0
  }
  const determinant = (
    shifted[0] * (shifted[1] * shifted[2] - coupling12 ** 2)
    - coupling01 * (coupling01 * shifted[2])
  ) / scale ** 3
  const angle = Math.acos(Math.max(-1, Math.min(1, determinant / 2))) / 3
  return 2 * scale * (Math.cos(angle) - Math.cos(angle + (2 * Math.PI) / 3))
}

function qutritPulsePayload(form: PulseLabForm): Record<string, unknown> {
  return form.shape === 'square'
    ? {
        shape: 'square',
        amplitude_mode: form.amplitudeMode,
        ...(form.amplitudeMode === 'target_rotation_angle'
          ? { target_rotation_angle_rad: form.targetRotationAngleRad }
          : { peak_amplitude_rad_per_us: form.peakAmplitudeRadPerUs }),
        pulse_duration_us: form.pulseDurationUs,
        phase_rad: form.phaseRad,
        detuning_rad_per_us: form.detuningRadPerUs,
        drag_beta_us: 0,
      }
    : {
        shape: 'gaussian',
        amplitude_mode: form.amplitudeMode,
        ...(form.amplitudeMode === 'target_rotation_angle'
          ? { target_rotation_angle_rad: form.targetRotationAngleRad }
          : { peak_amplitude_rad_per_us: form.peakAmplitudeRadPerUs }),
        sigma_us: form.sigmaUs,
        truncation_sigma: form.truncationSigma,
        phase_rad: form.phaseRad,
        detuning_rad_per_us: form.detuningRadPerUs,
        drag_beta_us: form.dragBetaUs,
      }
}

export function pulseWaveform(form: PulseLabForm, count = 121): PulseWaveformPoint[] {
  const duration = Math.max(0, pulseDurationUs(form))
  const peak = peakAmplitude(form)
  const cosine = Math.cos(form.phaseRad)
  const sine = Math.sin(form.phaseRad)
  const safeSigmaUs = Math.max(Math.abs(form.sigmaUs), 1e-12)
  return Array.from({ length: count }, (_, index) => {
    const timeUs = (duration * index) / (count - 1)
    let amplitude = peak
    let derivative = 0
    if (form.shape === 'gaussian') {
      const offset = timeUs - duration / 2
      amplitude = peak * Math.exp(-(offset * offset) / (2 * safeSigmaUs ** 2))
      derivative = (-offset / safeSigmaUs ** 2) * amplitude
    }
    const quadrature =
      form.modelId !== TWO_LEVEL_PULSE_MODEL && form.shape === 'gaussian'
        ? form.dragBetaUs * derivative
        : 0
    return {
      timeUs,
      omegaX: amplitude * cosine - quadrature * sine,
      omegaY: amplitude * sine + quadrature * cosine,
    }
  })
}

/**
 * Builds one physical control-channel trace from pulses placed on a common
 * timeline.  Zero-valued boundary points make inter-pulse idle periods (and
 * the end of the last pulse) visible in the SVG rather than joining two
 * unrelated pulse envelopes with a diagonal line.
 */
export function scheduledPulseWaveform(
  blocks: ReadonlyArray<{ startTimeUs: number; form: PulseLabForm }>,
  countPerPulse = 121,
): PulseWaveformPoint[] {
  const points: PulseWaveformPoint[] = []
  let previousEndUs = 0

  for (const block of [...blocks].sort((left, right) => left.startTimeUs - right.startTimeUs)) {
    const startTimeUs = Math.max(0, block.startTimeUs)
    const localPoints = pulseWaveform(block.form, countPerPulse)
    const durationUs = pulseDurationUs(block.form)
    const endTimeUs = startTimeUs + durationUs

    if (points.length === 0 || startTimeUs > previousEndUs + 1e-12) {
      points.push({ timeUs: previousEndUs, omegaX: 0, omegaY: 0 })
      points.push({ timeUs: startTimeUs, omegaX: 0, omegaY: 0 })
    }
    points.push(...localPoints.map((point) => ({
      ...point,
      timeUs: startTimeUs + point.timeUs,
    })))
    previousEndUs = Math.max(previousEndUs, endTimeUs)
  }

  if (points.length === 0) {
    return [{ timeUs: 0, omegaX: 0, omegaY: 0 }]
  }

  points.push({ timeUs: previousEndUs, omegaX: 0, omegaY: 0 })
  return points
}

export function sequentialPulseWaveform(
  forms: ReadonlyArray<PulseLabForm>,
  interPulseGapUs: number,
  countPerPulse = 121,
): PulseWaveformPoint[] {
  let startTimeUs = 0
  return scheduledPulseWaveform(forms.map((form, index) => {
    const block = { startTimeUs, form }
    startTimeUs += pulseDurationUs(form)
      + (index < forms.length - 1 ? Math.max(0, interPulseGapUs) : 0)
    return block
  }), countPerPulse)
}

/**
 * The network backend has one independent control channel per transmon.  The
 * waveform panel therefore renders the selected lane, with virtual-Z frame
 * updates applied exactly as they are when the network request is built.
 */
export function circuitLaneWaveform(
  globalForm: PulseLabForm,
  circuit: PulseCircuitState,
  transmonIndex: number,
  countPerPulse = 121,
): PulseWaveformPoint[] {
  const lane = circuit.lanes.find((candidate) => candidate.transmonIndex === transmonIndex)
  if (!lane) {
    return [{ timeUs: 0, omegaX: 0, omegaY: 0 }]
  }

  let startTimeUs = 0
  let framePhaseRad = 0
  const blocks: Array<{ startTimeUs: number; form: PulseLabForm }> = []
  lane.steps.forEach((step, stepIndex) => {
    if (!isDrivePulseStep(step)) {
      framePhaseRad = normalizeFramePhase(framePhaseRad + step.angleRad)
      return
    }

    const form = applyPulseStepToForm(globalForm, step.pulse)
    blocks.push({
      startTimeUs,
      form: {
        ...form,
        phaseRad: normalizeFramePhase(form.phaseRad + framePhaseRad),
      },
    })
    const hasLaterDrive = lane.steps.slice(stepIndex + 1).some(isDrivePulseStep)
    startTimeUs += pulseStepDurationUs(step.pulse)
      + (hasLaterDrive ? circuit.executionConstraints.interPulseGapUs : 0)
  })
  return scheduledPulseWaveform(blocks, countPerPulse)
}

export function estimatePulseCost(form: PulseLabForm): PulseCostEstimate {
  if (form.modelId === TWO_LEVEL_PULSE_MODEL) {
    const estimated = Math.max(form.snapshotCount, Math.ceil(form.totalSimulationTimeUs / 0.001))
    return costResult(estimated, TWO_LEVEL_API_MAX_STEPS)
  }

  const duration = pulseDurationUs(form)
  const peak = Math.abs(peakAmplitude(form))
  const dragMagnitude =
    form.shape === 'gaussian'
      ? Math.abs(form.dragBetaUs) * peak / Math.max(form.sigmaUs, 1e-12)
      : 0
  const drive = Math.hypot(peak, dragMagnitude)
  const detuning = form.quasiStaticNoiseEnabled
    ? Math.abs(form.detuningRadPerUs) + 5 * form.quasiStaticDetuningSigmaRadPerUs
    : form.detuningRadPerUs
  const diagonal = [
    0,
    -detuning,
    -2 * detuning + form.anharmonicityMhz * 2 * Math.PI,
  ]
  const diagonalSpan = Math.max(...diagonal) - Math.min(...diagonal)
  const hamiltonianScale = diagonalSpan + 2 * Math.SQRT2 * drive
  const dissipationScale =
    form.environmentMode === 'direct_rates'
      ? form.gamma10DownPerUs +
        form.gamma01UpPerUs +
        form.gamma21DownPerUs +
        form.gamma12UpPerUs +
        4 * form.gammaPhiAdjacentPerUs
      : 0
  const limits = [
    duration,
    EPSILON_H / Math.max(hamiltonianScale, 1e-12),
    ...(dissipationScale > 0 ? [EPSILON_D / dissipationScale] : []),
    ...(form.shape === 'gaussian' ? [form.sigmaUs / QUTRIT_SAMPLES_PER_SIGMA] : []),
  ]
  const step = Math.min(...limits.filter((value) => Number.isFinite(value) && value > 0))
  const singleEvolutionEstimate =
    Math.ceil(duration / step) +
    Math.ceil(Math.max(0, form.totalSimulationTimeUs - duration) / step) +
    form.snapshotCount
  const ensembleMultiplier = form.quasiStaticNoiseEnabled
    ? form.quasiStaticQuadratureOrder
    : 1
  const estimated = singleEvolutionEstimate * ensembleMultiplier
  return costResult(estimated, QUTRIT_API_MAX_STEPS)
}

function peakAmplitude(form: PulseLabForm): number {
  if (form.amplitudeMode === 'peak_amplitude') {
    return Number.isFinite(form.peakAmplitudeRadPerUs)
      ? form.peakAmplitudeRadPerUs
      : 0
  }
  if (form.shape === 'square') {
    return form.pulseDurationUs > 0 &&
      Number.isFinite(form.targetRotationAngleRad)
      ? form.targetRotationAngleRad / form.pulseDurationUs
      : 0
  }
  const areaFactor =
    form.sigmaUs *
    Math.sqrt(2 * Math.PI) *
    erf(form.truncationSigma / Math.SQRT2)
  return areaFactor > 0 && Number.isFinite(form.targetRotationAngleRad)
    ? form.targetRotationAngleRad / areaFactor
    : 0
}

function costResult(
  estimated: number,
  maximum: number,
  /*
   * CPTP は「区間数」で頭打ちになる。単位名と対処法が RK4 と違うため、
   * 超過メッセージだけ切り替える。
   */
  budgetKind: 'steps' | 'cptp_intervals' = 'steps',
): PulseCostEstimate {
  const ratio = estimated / maximum
  const overBudget = estimated > maximum
  const overBudgetMessage = budgetKind === 'cptp_intervals'
    ? `明示的 CPTP 写像の区間数が上限 ${maximum.toLocaleString()} を超えています`
      + `（推定 ${estimated.toLocaleString()} 区間）。`
      + 'シミュレーション時間を短くするか、パルス幅を広げるか、'
      + '固定ステップ RK4 に切り替えてください。'
    : `推定計算量が ${maximum.toLocaleString()} ステップのAPI上限を超えています。`
  return {
    estimatedInternalSteps: estimated,
    maximumInternalSteps: maximum,
    overBudget,
    level: overBudget ? 'blocked' : ratio >= 0.7 ? 'elevated' : 'normal',
    message: overBudget
      ? overBudgetMessage
      : ratio >= 0.7
        ? 'このリクエストはAPIの計算量上限に近く、数秒かかる場合があります。'
        : '推定計算量はAPIの実行上限内です。',
  }
}

function erf(value: number): number {
  const sign = value < 0 ? -1 : 1
  const x = Math.abs(value)
  const t = 1 / (1 + 0.3275911 * x)
  const polynomial =
    1 -
    (((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) *
      t +
      0.254829592) *
      t *
      Math.exp(-x * x))
  return sign * polynomial
}

function complexConjugate(value: { real: number; imag: number }) {
  return { real: value.real, imag: -value.imag }
}

function complexMultiply(
  left: { real: number; imag: number },
  right: { real: number; imag: number },
) {
  return {
    real: left.real * right.real - left.imag * right.imag,
    imag: left.real * right.imag + left.imag * right.real,
  }
}

export function qutritTargetOverlap(
  point: QutritPulsePoint,
  form: PulseLabForm,
): number | null {
  if (
    form.amplitudeMode !== 'target_rotation_angle' ||
    form.detuningRadPerUs !== 0
  ) {
    return null
  }
  const duration = pulseDurationUs(form)
  const fraction = Math.min(1, point.time_us / duration)
  const angle = form.targetRotationAngleRad * fraction
  const target = [
    { real: Math.cos(angle / 2), imag: 0 },
    {
      real: Math.sin(angle / 2) * Math.sin(form.phaseRad),
      imag: -Math.sin(angle / 2) * Math.cos(form.phaseRad),
    },
    { real: 0, imag: 0 },
  ]
  let overlap = 0
  for (let row = 0; row < 3; row += 1) {
    for (let column = 0; column < 3; column += 1) {
      const left = complexConjugate(target[row])
      const matrix = point.density_matrix[row][column]
      const right = target[column]
      overlap += complexMultiply(complexMultiply(left, matrix), right).real
    }
  }
  return Math.min(1, Math.max(0, overlap))
}

export function hasPulseResponseShape(value: unknown): value is PulseResponse {
  if (!value || typeof value !== 'object') {
    return false
  }
  const candidate = value as Record<string, unknown>
  return (
    typeof candidate.contract_version === 'string' &&
    Array.isArray(candidate.trajectory) &&
    typeof candidate.model === 'object' &&
    candidate.model !== null
  )
}
