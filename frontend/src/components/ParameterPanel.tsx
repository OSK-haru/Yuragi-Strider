import './ParameterPanel.css'
import { LabDial } from './LabDial'
import { SectionHeader } from './SectionHeader'
import type {
  GateDurationDefaultErrors,
  GateDurationDefaults,
  PerQubitCalibrationSettings,
  ReadoutErrorSettings,
  ReadoutErrorSettingsErrors,
  SimulateRequestParameterErrors,
  SimulateRequestParameters,
} from '../types/simulation'

type ParameterPanelProps = {
  editableParameters: SimulateRequestParameters
  gateDurationDefaults: GateDurationDefaults
  validationMessages: SimulateRequestParameterErrors
  gateDurationValidationMessages: GateDurationDefaultErrors
  readoutError: ReadoutErrorSettings
  readoutErrorValidationMessages: ReadoutErrorSettingsErrors
  logicalQubits: number
  perQubitCalibration: PerQubitCalibrationSettings
  perQubitCalibrationError: string | null
  onEditableParametersChange: (parameters: SimulateRequestParameters) => void
  onGateDurationDefaultsChange: (gateDurations: GateDurationDefaults) => void
  onReadoutErrorChange: (readoutError: ReadoutErrorSettings) => void
  onPerQubitCalibrationChange: (settings: PerQubitCalibrationSettings) => void
}

type EditableParameterName = keyof SimulateRequestParameters
type GateDurationName = keyof GateDurationDefaults

const parameterRanges: Record<
  EditableParameterName,
  { min: number; max?: number; step: number }
> = {
  device_quality: { min: 0, max: 1, step: 0.01 },
  temperature_mk: { min: 0, step: 1 },
  flux_noise_phi0: { min: 0, step: 0.000001 },
  qubit_frequency_ghz: { min: 0.001, step: 0.1 },
  t1_max_us: { min: 0.001, step: 1 },
  tphi_max_us: { min: 0.001, step: 1 },
  duration_us: { min: 0.001, step: 0.1 },
  time_steps: { min: 2, step: 1 },
  fidelity_threshold: { min: 0, max: 1, step: 0.01 },
}

const gateDurationRanges: Record<
  GateDurationName,
  { min: number; step: number }
> = {
  H: { min: 0.001, step: 0.01 },
  X: { min: 0.001, step: 0.01 },
  Y: { min: 0.001, step: 0.01 },
  Z: { min: 0, step: 0.01 },
  S: { min: 0, step: 0.01 },
  T: { min: 0, step: 0.01 },
  RX: { min: 0.001, step: 0.01 },
  RY: { min: 0.001, step: 0.01 },
  RZ: { min: 0.001, step: 0.01 },
  CNOT: { min: 0.001, step: 0.01 },
  CZ: { min: 0.001, step: 0.01 },
  SWAP: { min: 0.001, step: 0.01 },
  CP: { min: 0.001, step: 0.01 },
  CCX: { min: 0.001, step: 0.01 },
  QFT: { min: 0.001, step: 0.01 },
  ORACLE: { min: 0.001, step: 0.01 },
  MEASURE: { min: 0, step: 0.01 },
  RECEIVED: { min: 0, step: 0.01 },
  MESSAGE: { min: 0.04, step: 0.01 },
}

type DialConfig = {
  dialMax: number
  unit?: string
  formatValue?: (value: number) => string
}

const dialConfigs: Partial<Record<EditableParameterName, DialConfig>> = {
  device_quality: { dialMax: 1, formatValue: (value) => value.toFixed(2) },
  qubit_frequency_ghz: { dialMax: 10, unit: 'GHz', formatValue: (value) => value.toFixed(2) },
  t1_max_us: { dialMax: 200, unit: 'μs', formatValue: (value) => value.toFixed(1) },
  tphi_max_us: { dialMax: 200, unit: 'μs', formatValue: (value) => value.toFixed(1) },
  temperature_mk: { dialMax: 100, unit: 'mK', formatValue: (value) => value.toFixed(1) },
  flux_noise_phi0: { dialMax: 0.00001, unit: 'Φ0', formatValue: (value) => value.toExponential(2) },
}

type GateDurationGroup = {
  title: string
  note: string
  gates: Array<{ name: GateDurationName; label: string }>
}

const gateDurationGroups: GateDurationGroup[] = [
  {
    title: '回転ゲート(物理パルス)',
    note: '有限のパルス長を持つ1量子ビット回転ゲートです。',
    gates: [
      { name: 'H', label: 'H' },
      { name: 'X', label: 'X' },
      { name: 'Y', label: 'Y' },
      { name: 'RX', label: 'RX' },
      { name: 'RY', label: 'RY' },
      { name: 'RZ', label: 'RZ' },
    ],
  },
  {
    title: '仮想位相ゲート(瞬時)',
    note: 'フレーム更新で実装され、既定では所要時間 0 です。',
    gates: [
      { name: 'Z', label: 'Z' },
      { name: 'S', label: 'S' },
      { name: 'T', label: 'T' },
    ],
  },
  {
    title: '複数量子ビットゲート',
    note: '2量子ビット以上の結合操作です。',
    gates: [
      { name: 'CNOT', label: 'CNOT' },
      { name: 'CZ', label: 'CZ' },
      { name: 'SWAP', label: 'SWAP' },
      { name: 'CP', label: 'CP' },
      { name: 'CCX', label: 'CCX' },
    ],
  },
  {
    title: 'レジスタゲート',
    note: 'またぐ量子ビット数が可変のため、1量子ビットあたりの所要時間を指定します。',
    gates: [
      { name: 'QFT', label: 'QFT (1量子ビットあたり)' },
      { name: 'ORACLE', label: 'ORACLE (1量子ビットあたり)' },
    ],
  },
  {
    title: '測定',
    note: '測定操作の所要時間です。',
    gates: [{ name: 'MEASURE', label: '測定' }],
  },
]

function clampParameterValue(name: EditableParameterName, value: number) {
  const range = parameterRanges[name]
  const maxClamped = range.max === undefined ? value : Math.min(value, range.max)
  const minClamped = Math.max(maxClamped, range.min)
  return name === 'time_steps' ? Math.round(minClamped) : minClamped
}

function clampGateDurationValue(name: GateDurationName, value: number) {
  const range = gateDurationRanges[name]
  return Math.max(value, range.min)
}

export function ParameterPanel({
  editableParameters,
  gateDurationDefaults,
  validationMessages,
  gateDurationValidationMessages,
  readoutError,
  readoutErrorValidationMessages,
  logicalQubits,
  perQubitCalibration,
  perQubitCalibrationError,
  onEditableParametersChange,
  onGateDurationDefaultsChange,
  onReadoutErrorChange,
  onPerQubitCalibrationChange,
}: ParameterPanelProps) {
  function updateParameter(name: EditableParameterName, value: number) {
    if (!Number.isFinite(value)) {
      return
    }

    onEditableParametersChange({
      ...editableParameters,
      [name]: clampParameterValue(name, value),
    })
  }

  function updateGateDuration(name: GateDurationName, value: number) {
    if (!Number.isFinite(value)) {
      return
    }

    onGateDurationDefaultsChange({
      ...gateDurationDefaults,
      [name]: clampGateDurationValue(name, value),
    })
  }

  function renderNumberInput(
    name: EditableParameterName,
    label: string,
    hint: string,
  ) {
    const range = parameterRanges[name]
    const validationMessage = validationMessages[name]
    return (
      /* チュートリアルがこの項目を名指しで指せるようにしておく。 */
      <label className="parameter-panel__field" data-tutorial-anchor={`param-${name}`}>
        <span className="parameter-panel__field-label">{label}</span>
        <input
          className={`parameter-panel__input${
            validationMessage ? ' parameter-panel__input--invalid' : ''
          }`}
          type="number"
          min={range.min}
          max={range.max}
          step={range.step}
          value={editableParameters[name]}
          aria-invalid={validationMessage ? 'true' : undefined}
          onChange={(event) => updateParameter(name, event.currentTarget.valueAsNumber)}
        />
        <span className="parameter-panel__hint">{hint}</span>
        {validationMessage ? (
          <span className="parameter-panel__validation">{validationMessage}</span>
        ) : null}
      </label>
    )
  }

  function renderDial(name: EditableParameterName, label: string, hint: string) {
    const range = parameterRanges[name]
    const config = dialConfigs[name]
    if (!config) {
      return renderNumberInput(name, label, hint)
    }

    return (
      <LabDial
        key={name}
        anchor={`param-${name}`}
        label={label}
        value={editableParameters[name]}
        min={range.min}
        max={config.dialMax}
        step={range.step}
        unit={config.unit}
        hint={hint}
        validationMessage={validationMessages[name]}
        formatValue={config.formatValue}
        onChange={(nextValue) => updateParameter(name, nextValue)}
      />
    )
  }

  function renderGateDurationInput(name: GateDurationName, label: string) {
    const range = gateDurationRanges[name]
    const validationMessage = gateDurationValidationMessages[name]
    return (
      <label className="parameter-panel__field" key={name}>
        <span className="parameter-panel__field-label">{label}</span>
        <input
          className={`parameter-panel__input${
            validationMessage ? ' parameter-panel__input--invalid' : ''
          }`}
          type="number"
          min={range.min}
          step={range.step}
          value={gateDurationDefaults[name]}
          aria-invalid={validationMessage ? 'true' : undefined}
          onChange={(event) => updateGateDuration(name, event.currentTarget.valueAsNumber)}
        />
        {validationMessage ? (
          <span className="parameter-panel__validation">{validationMessage}</span>
        ) : null}
      </label>
    )
  }

  return (
    <section
      className="parameter-panel"
      aria-label="シミュレーションパラメーター"
      data-tutorial-anchor="parameter-panel"
    >
      <div className="parameter-panel__header">
        <SectionHeader
          icon="chip"
          eyebrow="パラメーター"
          title="シミュレーションパラメーター"
        />
        <p className="parameter-panel__note">
          校正済みのハードウェアモデルではなく、学習用の一般的なプロファイルです。
        </p>
      </div>

      <div className="parameter-panel__sections">
        <article className="parameter-panel__section">
          <SectionHeader
            className="parameter-panel__section-header"
            icon="chip"
            title="デバイス"
            headingLevel="h3"
          />
          <div className="parameter-panel__fields parameter-panel__fields--dials">
            {renderDial(
              'device_quality',
              'デバイス品質',
              '学習用デバイスモデルで使用する 0〜1 の抽象的なプロファイル値です。',
            )}
            {renderDial(
              'qubit_frequency_ghz',
              '量子ビット周波数',
              '0 より大きい値',
            )}
            {renderDial(
              't1_max_us',
              '最大 T1',
              '0 より大きい値',
            )}
            {renderDial(
              'tphi_max_us',
              '最大 Tφ',
              '0 より大きい値',
            )}
          </div>
        </article>

        <article className="parameter-panel__section">
          <SectionHeader
            className="parameter-panel__section-header"
            icon="thermometer"
            title="環境"
            headingLevel="h3"
          />
          <div className="parameter-panel__fields parameter-panel__fields--dials">
            {renderDial(
              'temperature_mk',
              '温度',
              '0 以上',
            )}
            {renderDial(
              'flux_noise_phi0',
              '磁束ノイズ',
              '0 以上',
            )}
          </div>
        </article>

        <article className="parameter-panel__section">
          <SectionHeader
            className="parameter-panel__section-header"
            icon="clock"
            title="シミュレーション時間"
            headingLevel="h3"
          />
          <div className="parameter-panel__fields">
            {renderNumberInput(
              'duration_us',
              '総シミュレーション時間 [μs]',
              'ゲート操作時間と、回路完了後の待機・観測時間を含みます。',
            )}
            {renderNumberInput('time_steps', '時間ステップ数', '2 以上の整数')}
            {renderNumberInput(
              'fidelity_threshold',
              '忠実度のしきい値',
              '0.0〜1.0',
            )}
          </div>
        </article>

        <article className="parameter-panel__section">
          <SectionHeader
            className="parameter-panel__section-header"
            icon="bars"
            title="読み出し誤差"
            headingLevel="h3"
          />
          <p className="parameter-panel__section-note">
            終端の観測確率と測定カウントだけに適用します。密度行列、忠実度、純度には影響しません。
          </p>
          <div className="parameter-panel__fields parameter-panel__fields--compact">
            <label className="parameter-panel__field">
              <span className="parameter-panel__field-label">観測モデル</span>
              <select
                className="parameter-panel__input"
                value={readoutError.enabled ? 'enabled' : 'disabled'}
                onChange={(event) => onReadoutErrorChange({
                  ...readoutError,
                  enabled: event.currentTarget.value === 'enabled',
                })}
              >
                <option value="disabled">無効</option>
                <option value="enabled">有効</option>
              </select>
            </label>
            <label className="parameter-panel__field">
              <span className="parameter-panel__field-label">p10 = P(観測1 | 真の0)</span>
              <input
                className={`parameter-panel__input${readoutErrorValidationMessages.p10 ? ' parameter-panel__input--invalid' : ''}`}
                type="number"
                min={0}
                max={1}
                step={0.001}
                value={readoutError.p10}
                disabled={!readoutError.enabled}
                onChange={(event) => onReadoutErrorChange({ ...readoutError, p10: event.currentTarget.valueAsNumber })}
              />
              {readoutErrorValidationMessages.p10 ? <span className="parameter-panel__validation">{readoutErrorValidationMessages.p10}</span> : null}
            </label>
            <label className="parameter-panel__field">
              <span className="parameter-panel__field-label">p01 = P(観測0 | 真の1)</span>
              <input
                className={`parameter-panel__input${readoutErrorValidationMessages.p01 ? ' parameter-panel__input--invalid' : ''}`}
                type="number"
                min={0}
                max={1}
                step={0.001}
                value={readoutError.p01}
                disabled={!readoutError.enabled}
                onChange={(event) => onReadoutErrorChange({ ...readoutError, p01: event.currentTarget.valueAsNumber })}
              />
              {readoutErrorValidationMessages.p01 ? <span className="parameter-panel__validation">{readoutErrorValidationMessages.p01}</span> : null}
            </label>
          </div>
          {readoutErrorValidationMessages.span ? <p className="parameter-panel__validation">{readoutErrorValidationMessages.span}</p> : null}
        </article>

        <article className="parameter-panel__section">
          <SectionHeader
            className="parameter-panel__section-header"
            icon="atom"
            title="量子ビット別校正値"
            headingLevel="h3"
          />
          <p className="parameter-panel__section-note">
            共通値を量子ビットごとの T1・Tφ・読み出し誤差で上書きします。
            読み出し列は上の観測モデルが有効な場合だけ適用されます。
          </p>
          <label className="parameter-panel__field parameter-panel__per-qubit-toggle">
            <span className="parameter-panel__field-label">個別校正</span>
            <select
              className="parameter-panel__input"
              value={perQubitCalibration.enabled ? 'enabled' : 'disabled'}
              onChange={(event) => onPerQubitCalibrationChange({
                ...perQubitCalibration,
                enabled: event.currentTarget.value === 'enabled',
              })}
            >
              <option value="disabled">共通値を使用</option>
              <option value="enabled">量子ビット別に設定</option>
            </select>
          </label>
          {perQubitCalibration.enabled ? (
            <div className="parameter-panel__qubit-calibrations">
              {perQubitCalibration.values.slice(0, logicalQubits).map((entry, qubit) => (
                <div className="parameter-panel__qubit-calibration" key={qubit}>
                  <strong>q{qubit}</strong>
                  {(['t1_max_us', 'tphi_max_us', 'p10', 'p01'] as const).map((name) => (
                    <label className="parameter-panel__field" key={name}>
                      <span className="parameter-panel__field-label">
                        {name === 't1_max_us' ? 'T1 max [μs]' : name === 'tphi_max_us' ? 'Tφ max [μs]' : name}
                      </span>
                      <input
                        className="parameter-panel__input"
                        type="number"
                        min={name === 'p10' || name === 'p01' ? 0 : 0.001}
                        max={name === 'p10' || name === 'p01' ? 1 : undefined}
                        step={name === 'p10' || name === 'p01' ? 0.001 : 1}
                        value={entry[name]}
                        disabled={(name === 'p10' || name === 'p01') && !readoutError.enabled}
                        onChange={(event) => {
                          const values = perQubitCalibration.values.map((value, index) => (
                            index === qubit
                              ? { ...value, [name]: event.currentTarget.valueAsNumber }
                              : value
                          ))
                          onPerQubitCalibrationChange({ ...perQubitCalibration, values })
                        }}
                      />
                    </label>
                  ))}
                </div>
              ))}
            </div>
          ) : null}
          {perQubitCalibrationError ? (
            <p className="parameter-panel__validation">{perQubitCalibrationError}</p>
          ) : null}
        </article>

        <details className="parameter-panel__section parameter-panel__section--collapsible">
          <summary className="parameter-panel__section-summary">
            <SectionHeader
              className="parameter-panel__section-header"
              icon="stopwatch"
              title="ゲート時間のデフォルト"
              headingLevel="h3"
            />
            <span className="parameter-panel__section-toggle" aria-hidden="true" />
          </summary>
          <p className="parameter-panel__section-note">
            現在のプリセットで各ゲート種別に使用するデフォルトの操作時間です。
            ゲートの種類ごとにグループ化しています。
          </p>
          <div className="parameter-panel__gate-groups">
            {gateDurationGroups.map((group) => (
              <div className="parameter-panel__gate-group" key={group.title}>
                <h4 className="parameter-panel__gate-group-title">{group.title}</h4>
                <p className="parameter-panel__gate-group-note">{group.note}</p>
                <div className="parameter-panel__fields parameter-panel__fields--compact">
                  {group.gates.map(({ name, label }) =>
                    renderGateDurationInput(name, `${label} [μs]`),
                  )}
                </div>
              </div>
            ))}
          </div>
        </details>
      </div>
    </section>
  )
}
