import type {
  PulseLabErrors,
  PulseLabForm,
  QuasiStaticQuadratureOrder,
} from '../types/pulse'
import type { PulseExecutionConstraints } from '../types/pulseCircuit'
import {
  matchingPulseDeviceProfile,
  pulseDeviceProfileConstraints,
  pulseDeviceProfiles,
} from '../utils/pulseDeviceProfiles'
import { useInternalInfoVisible } from '../context/useAdminMode'
import './PulseEnvironmentPanel.css'

const PULSE_LAB_RUNNABLE_TRANSMON_MAX = 4

type PulseEnvironmentPanelProps = {
  form: PulseLabForm
  errors: PulseLabErrors
  disabled: boolean
  transmonCount: number
  onTransmonCountChange: (count: number) => void
  onChange: (next: PulseLabForm) => void
  executionConstraints: PulseExecutionConstraints
  onExecutionConstraintsChange: (next: PulseExecutionConstraints) => void
}

export function PulseEnvironmentPanel({
  form,
  errors,
  disabled,
  transmonCount,
  onTransmonCountChange,
  onChange,
  executionConstraints,
  onExecutionConstraintsChange,
}: PulseEnvironmentPanelProps) {
  const internalInfoVisible = useInternalInfoVisible()
  const isQutrit = form.localLevels === 3
  const isNetwork = transmonCount >= 2
  /*
   * 直接レート入力の項目数はモデルの準位数で決まる。2準位×1台だけが
   * down / up / φ の3レート、それ以外（qutrit・ネットワーク）は遷移ごとの
   * 5レート。ここを取り違えると、画面で編集した値と送信するペイロードが
   * 食い違ったまま気付けない。deriveModelId と同じ条件で分ける。
   */
  const usesTwoLevelRates = form.localLevels === 2 && transmonCount === 1
  const cptpAvailable = form.localLevels ** transmonCount <= 9
  const activeDeviceProfile = matchingPulseDeviceProfile(executionConstraints)
  const setNumber = (field: keyof PulseLabForm, value: number) => {
    onChange({ ...form, [field]: value })
  }
  const setConstraint = (field: keyof PulseExecutionConstraints, value: number) => {
    onExecutionConstraintsChange({ ...executionConstraints, [field]: value })
  }

  return (
    <section className="pulse-parameters" aria-labelledby="pulse-environment-title">
      <div className="pulse-parameters__heading">
        <div>
          <span>実行条件(共通)</span>
          <h2 id="pulse-environment-title">外界環境と実行設定</h2>
        </div>
        <p>ここで設定した条件は、Pulse回路内のすべてのブロックに共通で適用されます。</p>
      </div>

      <div className="pulse-parameters__selectors">
        <label>
          準位数
          <select
            value={form.localLevels}
            disabled={disabled}
            onChange={(event) => {
              const localLevels = Number(event.target.value) as PulseLabForm['localLevels']
              onChange({
                ...form,
                localLevels,
                /* 2準位には漏れ準位がないので DRAG は無効。 */
                dragBetaUs: localLevels === 2 ? 0 : form.dragBetaUs,
                /* CPTP が使えない次元へ移ったら RK4 に戻す。 */
                evolutionMethod: localLevels ** transmonCount <= 9
                  ? form.evolutionMethod
                  : 'fixed_step_rk4',
              })
            }}
          >
            <option value={2}>2準位（軽量・漏れなし）</option>
            <option value={3}>3準位 qutrit（漏れ準位あり）</option>
          </select>
        </label>
        <label>
          トランズモン数
          <select
            value={Math.min(transmonCount, PULSE_LAB_RUNNABLE_TRANSMON_MAX)}
            disabled={disabled}
            onChange={(event) => onTransmonCountChange(Number(event.target.value))}
          >
            {[1, 2, 3, 4].map((count) => (
              <option value={count} key={count}>{count}</option>
            ))}
          </select>
        </label>
        <p className="pulse-parameters__model-note">
          {transmonCount > PULSE_LAB_RUNNABLE_TRANSMON_MAX
            ? `回路スタジオで ${transmonCount} 台に設定されています。Pulse ラボで実行できるのは 4 台までです。`
            : transmonCount >= 2
            ? `${transmonCount} トランズモン・ネットワーク / ${form.localLevels}^${transmonCount} = ${form.localLevels ** transmonCount} 次元。`
            : `1 トランズモン / ${form.localLevels} 次元。2 台以上でネットワーク（交換結合あり）に切り替わります。`}
        </p>
        <label>
          環境入力
          <select
            value={form.environmentMode}
            disabled={disabled}
            onChange={(event) => onChange({
              ...form,
              environmentMode: event.target.value as PulseLabForm['environmentMode'],
            })}
          >
            <option value="physical">物理パラメータ</option>
            <option value="direct_rates">Lindblad率を直接指定</option>
          </select>
        </label>
        <label>
          時間発展の解法
          <select
            value={form.evolutionMethod}
            disabled={disabled}
            onChange={(event) => onChange({
              ...form,
              evolutionMethod: event.target.value as PulseLabForm['evolutionMethod'],
            })}
          >
            <option value="fixed_step_rk4">固定ステップ RK4</option>
            <option value="explicit_cptp" disabled={!cptpAvailable}>
              明示的 CPTP 写像{cptpAvailable ? '' : '（9次元以下のみ）'}
            </option>
          </select>
        </label>
        {/*
          実行基盤の選択は実装名がそのまま並ぶため詳細モード専用。
          通常は自動選択（既定値）のまま走る。
        */}
        {internalInfoVisible ? (
          <label>
            バックエンド
            <select
              value={form.backend}
              disabled={disabled}
              onChange={(event) => onChange({
                ...form,
                backend: event.target.value as PulseLabForm['backend'],
              })}
            >
              <option value="auto">Auto（Rust優先）</option>
              <option value="rust">Rust dense</option>
              <option value="python">Python dense</option>
            </select>
          </label>
        ) : null}
      </div>

      <div className="pulse-parameters__grid">
        <NumberField field="totalSimulationTimeUs" label="観測時間 [us]" value={form.totalSimulationTimeUs} error={errors.totalSimulationTimeUs} step={0.001} min={0} disabled={disabled} onChange={setNumber} />
        <NumberField field="snapshotCount" label="スナップショット数" value={form.snapshotCount} error={errors.snapshotCount} step={1} min={2} max={1001} disabled={disabled} onChange={setNumber} />
        {/* 非調和性は2準位でも（ステップ方針決定用に）必須なので、常に出す。 */}
        <NumberField field="anharmonicityMhz" label="非調和性 [MHz]" value={form.anharmonicityMhz} error={errors.anharmonicityMhz} step={10} max={0} disabled={disabled} onChange={setNumber} />
      </div>

      {isNetwork ? (
        <div className="pulse-parameters__environment">
          <h3>トランズモン・ネットワーク（{transmonCount} 台）</h3>
          <p className="pulse-parameters__model-note">
            回路スタジオの {transmonCount} レーンを同時に実行します。各トランズモンは
            {form.localLevels} 準位で、隣接レーン間に同じ交換結合 J を適用します。
          </p>
          <div className="pulse-parameters__grid">
            <NumberField field="networkDetuningQ0RadPerUs" label="q0 基準離調 [rad/us]" value={form.networkDetuningQ0RadPerUs} error={errors.networkDetuningQ0RadPerUs} step={1} disabled={disabled} onChange={setNumber} />
            <NumberField field="networkDetuningQ1RadPerUs" label="q1 基準離調 [rad/us]" value={form.networkDetuningQ1RadPerUs} error={errors.networkDetuningQ1RadPerUs} step={1} disabled={disabled} onChange={setNumber} />
            <NumberField field="networkExchangeCouplingRadPerUs" label="隣接交換結合 J [rad/us]" value={form.networkExchangeCouplingRadPerUs} error={errors.networkExchangeCouplingRadPerUs} step={0.5} min={0} disabled={disabled} onChange={setNumber} />
          </div>
          <h3>量子ビット別校正値</h3>
          <p className="pulse-parameters__model-note">
            共通の最大 T1・Tφ・読み出し誤差を、各トランズモンの値で上書きします。
          </p>
          <label className="pulse-parameters__calibration-toggle">
            <span>個別校正</span>
            <select
              value={form.networkPerQubitCalibrationEnabled ? 'enabled' : 'disabled'}
              disabled={disabled || form.environmentMode !== 'physical'}
              onChange={(event) => onChange({
                ...form,
                networkPerQubitCalibrationEnabled: event.target.value === 'enabled',
              })}
            >
              <option value="disabled">共通値を使用</option>
              <option value="enabled">トランズモン別に設定</option>
            </select>
          </label>
          {form.networkPerQubitCalibrationEnabled && form.environmentMode === 'physical' ? (
            <div className="pulse-parameters__calibration-table">
              {Array.from({ length: transmonCount }, (_, index) => (
                <div className="pulse-parameters__calibration-row" key={index}>
                  <strong>q{index}</strong>
                  {([
                    ['networkT1MaxUs', '最大 T1 [us]', form.networkT1MaxUs, 0.001, undefined],
                    ['networkTphiMaxUs', '最大 Tφ [us]', form.networkTphiMaxUs, 0.001, undefined],
                    ['networkReadoutP10', 'p10', form.networkReadoutP10, 0, 1],
                    ['networkReadoutP01', 'p01', form.networkReadoutP01, 0, 1],
                  ] as const).map(([field, label, values, min, max]) => (
                    <label key={field}>
                      <span>{label}</span>
                      <input
                        type="number"
                        min={min}
                        max={max}
                        step={field.includes('Readout') ? 0.001 : 1}
                        value={values[index]}
                        disabled={disabled || (field.includes('Readout') && !form.readoutErrorEnabled)}
                        onChange={(event) => {
                          const nextValues = [...values]
                          nextValues[index] = event.target.valueAsNumber
                          onChange({ ...form, [field]: nextValues })
                        }}
                      />
                    </label>
                  ))}
                </div>
              ))}
              {errors.networkT1MaxUs || errors.networkTphiMaxUs || errors.networkReadoutP01 ? (
                <p className="pulse-parameters__error">
                  {errors.networkT1MaxUs ?? errors.networkTphiMaxUs ?? errors.networkReadoutP01}
                </p>
              ) : null}
            </div>
          ) : null}
          <p className="pulse-parameters__model-note">
            q2 以降の基準離調は現在 0 です。密度行列は {form.localLevels}<sup>N</sup> 次元になるため、
            台数に応じて計算量とスナップショット数を自動制限します。
            {cptpAvailable
              ? ' この次元では明示的 CPTP 写像も選べます。'
              : ' 明示的 CPTP 写像は 9 次元以下のときのみ選べます。'}
          </p>

          <h3>相関準静的離調ノイズ</h3>
          <p className="pulse-parameters__model-note">
            全トランズモンに共通の σ を与え、隣接ペアに共通の相関係数 r を掛けます。
            σ = 0 のときはアンサンブル平均を行いません（{form.networkQuasiStaticQuadratureOrder}
            <sup>{transmonCount}</sup> 個の軌道を重み付き平均）。
          </p>
          <div className="pulse-parameters__grid">
            <NumberField field="networkQuasiStaticSigmaRadPerUs" label="共通 σ [rad/us]" value={form.networkQuasiStaticSigmaRadPerUs} error={errors.networkQuasiStaticSigmaRadPerUs} step={0.5} min={0} disabled={disabled} onChange={setNumber} />
            <NumberField field="networkQuasiStaticAdjacentCorrelation" label="隣接相関係数 r" value={form.networkQuasiStaticAdjacentCorrelation} error={errors.networkQuasiStaticAdjacentCorrelation} step={0.1} min={-1} max={1} disabled={disabled || form.networkQuasiStaticSigmaRadPerUs <= 0} onChange={setNumber} />
            <label>
              <span>求積次数 / 軸</span>
              <select
                value={form.networkQuasiStaticQuadratureOrder}
                disabled={disabled || form.networkQuasiStaticSigmaRadPerUs <= 0}
                onChange={(event) => onChange({
                  ...form,
                  networkQuasiStaticQuadratureOrder: Number(event.target.value) as 3 | 5,
                })}
              >
                <option value={3}>3</option>
                <option value={5}>5</option>
              </select>
            </label>
          </div>
        </div>
      ) : null}

      <div className="pulse-parameters__environment">
        <h3>実行ハードウェア制約</h3>
        <div className="pulse-parameters__device-profile">
          <label>
            デバイスプロファイル
            <select
              value={activeDeviceProfile?.id ?? 'custom'}
              disabled={disabled}
              onChange={(event) => {
                const next = pulseDeviceProfileConstraints(event.target.value)
                if (next) onExecutionConstraintsChange(next)
              }}
            >
              {pulseDeviceProfiles.map((profile) => (
                <option value={profile.id} key={profile.id}>{profile.name}</option>
              ))}
              <option value="custom" disabled>カスタム</option>
            </select>
          </label>
          <p>
            {activeDeviceProfile?.description
              ?? '各制約を個別に変更したカスタム設定です。'}
            <strong> 実機校正値ではありません。</strong>
          </p>
        </div>
        <div className="pulse-parameters__grid">
          <ConstraintField field="maximumDriveAmplitudeRadPerUs" label="最大駆動振幅 [rad/us]" value={executionConstraints.maximumDriveAmplitudeRadPerUs} step={10} min={0} disabled={disabled} onChange={setConstraint} />
          <ConstraintField field="minimumPulseDurationUs" label="最小パルス幅 [us]" value={executionConstraints.minimumPulseDurationUs} step={0.001} min={0} disabled={disabled} onChange={setConstraint} />
          <ConstraintField field="awgSamplePeriodUs" label="AWGサンプル周期 [us]" value={executionConstraints.awgSamplePeriodUs} step={0.0001} min={0} disabled={disabled} onChange={setConstraint} />
          <ConstraintField field="phaseResolutionRad" label="位相分解能 [rad]" value={executionConstraints.phaseResolutionRad} step={0.001} min={0} disabled={disabled} onChange={setConstraint} />
          <ConstraintField field="amplitudeResolutionRadPerUs" label="振幅分解能 [rad/us]" value={executionConstraints.amplitudeResolutionRadPerUs} step={0.01} min={0} disabled={disabled} onChange={setConstraint} />
          <ConstraintField field="maximumDetuningRadPerUs" label="最大離調 [rad/us]" value={executionConstraints.maximumDetuningRadPerUs} step={10} min={0} disabled={disabled} onChange={setConstraint} />
          <ConstraintField field="interPulseGapUs" label="パルス間待機 [us]" value={executionConstraints.interPulseGapUs} step={0.001} min={0} disabled={disabled} onChange={setConstraint} />
        </div>
      </div>

      {isQutrit ? (
        <div className="pulse-parameters__environment">
          <h3>準静的ノイズ</h3>
          <p className="pulse-parameters__model-note">
            1回のショット中は一定で、ショット間でGaussian分布する周波数離調を密度行列のアンサンブル平均として計算します。
          </p>
          <div className="pulse-parameters__selectors">
            <label>
              <span>準静的離調ノイズ</span>
              <select
                value={form.quasiStaticNoiseEnabled ? 'enabled' : 'disabled'}
                disabled={disabled}
                onChange={(event) => onChange({
                  ...form,
                  quasiStaticNoiseEnabled: event.target.value === 'enabled',
                })}
              >
                <option value="disabled">無効</option>
                <option value="enabled">有効</option>
              </select>
            </label>
            <NumberField
              field="quasiStaticDetuningSigmaRadPerUs"
              label="離調の標準偏差 σ [rad/us]"
              value={form.quasiStaticDetuningSigmaRadPerUs}
              error={errors.quasiStaticDetuningSigmaRadPerUs}
              step={0.1}
              min={0}
              disabled={disabled || !form.quasiStaticNoiseEnabled}
              onChange={setNumber}
            />
            <label>
              <span>Gauss-Hermite求積次数</span>
              <select
                value={form.quasiStaticQuadratureOrder}
                disabled={disabled || !form.quasiStaticNoiseEnabled}
                onChange={(event) => onChange({
                  ...form,
                  quasiStaticQuadratureOrder: Number(event.target.value) as QuasiStaticQuadratureOrder,
                })}
              >
                {[3, 5, 7, 9].map((order) => <option key={order} value={order}>{order}</option>)}
              </select>
            </label>
          </div>
        </div>
      ) : null}

      <div className="pulse-parameters__environment">
        <h3>読み出し誤差</h3>
        <p className="pulse-parameters__model-note">
          最終観測と確率表示だけに適用します。状態、純度、忠実度、リーク確率は変更しません。
          3準位では |2⟩ を保持し、|0⟩ と |1⟩ の間だけを誤分類します。
        </p>
        <div className="pulse-parameters__selectors">
          <label>
            <span>観測モデル</span>
            <select
              value={form.readoutErrorEnabled ? 'enabled' : 'disabled'}
              disabled={disabled}
              onChange={(event) => onChange({
                ...form,
                readoutErrorEnabled: event.target.value === 'enabled',
              })}
            >
              <option value="disabled">無効</option>
              <option value="enabled">有効</option>
            </select>
          </label>
          <NumberField field="readoutP10" label="p10 = P(観測1 | 真の0)" value={form.readoutP10} error={errors.readoutP10} step={0.001} min={0} max={1} disabled={disabled || !form.readoutErrorEnabled} onChange={setNumber} />
          <NumberField field="readoutP01" label="p01 = P(観測0 | 真の1)" value={form.readoutP01} error={errors.readoutP01} step={0.001} min={0} max={1} disabled={disabled || !form.readoutErrorEnabled} onChange={setNumber} />
        </div>
      </div>

      <div className="pulse-parameters__environment">
        <h3>{form.environmentMode === 'physical' ? '外界の物理環境' : '外界との結合率'}</h3>
        {form.environmentMode === 'physical' ? (
          <div className="pulse-parameters__grid">
            <NumberField field="deviceQuality" label="デバイス品質" value={form.deviceQuality} error={errors.deviceQuality} step={0.05} min={0} max={1} disabled={disabled} onChange={setNumber} />
            <NumberField field="temperatureMk" label="温度 [mK]" value={form.temperatureMk} error={errors.temperatureMk} step={1} min={0} disabled={disabled} onChange={setNumber} />
            <NumberField field="fluxNoisePhi0" label="磁束ノイズ [Phi0]" value={form.fluxNoisePhi0} error={errors.fluxNoisePhi0} step={0.000001} min={0} disabled={disabled} onChange={setNumber} />
            <NumberField field="qubitFrequencyGhz" label="量子ビット周波数 [GHz]" value={form.qubitFrequencyGhz} error={errors.qubitFrequencyGhz} step={0.1} min={0} disabled={disabled} onChange={setNumber} />
            <NumberField field="t1MaxUs" label="最大 T1 [us]" value={form.t1MaxUs} error={errors.t1MaxUs} step={1} min={0} disabled={disabled} onChange={setNumber} />
            <NumberField field="tphiMaxUs" label="最大 Tphi [us]" value={form.tphiMaxUs} error={errors.tphiMaxUs} step={1} min={0} disabled={disabled} onChange={setNumber} />
          </div>
        ) : (
          <div className="pulse-parameters__grid">
            {usesTwoLevelRates ? (
              <>
                <NumberField field="gammaDownPerUs" label="gamma down [1/us]" value={form.gammaDownPerUs} error={errors.gammaDownPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
                <NumberField field="gammaUpPerUs" label="gamma up [1/us]" value={form.gammaUpPerUs} error={errors.gammaUpPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
                <NumberField field="gammaPhiPerUs" label="gamma phi [1/us]" value={form.gammaPhiPerUs} error={errors.gammaPhiPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
              </>
            ) : (
              <>
                <NumberField field="gamma10DownPerUs" label="gamma 10 down [1/us]" value={form.gamma10DownPerUs} error={errors.gamma10DownPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
                <NumberField field="gamma01UpPerUs" label="gamma 01 up [1/us]" value={form.gamma01UpPerUs} error={errors.gamma01UpPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
                <NumberField field="gamma21DownPerUs" label="gamma 21 down [1/us]" value={form.gamma21DownPerUs} error={errors.gamma21DownPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
                <NumberField field="gamma12UpPerUs" label="gamma 12 up [1/us]" value={form.gamma12UpPerUs} error={errors.gamma12UpPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
                <NumberField field="gammaPhiAdjacentPerUs" label="隣接準位の位相緩和 [1/us]" value={form.gammaPhiAdjacentPerUs} error={errors.gammaPhiAdjacentPerUs} step={0.01} min={0} disabled={disabled} onChange={setNumber} />
              </>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

type ConstraintFieldProps = {
  field: keyof PulseExecutionConstraints
  label: string
  value: number
  step: number
  min: number
  disabled: boolean
  onChange: (field: keyof PulseExecutionConstraints, value: number) => void
}

function ConstraintField({ field, label, value, step, min, disabled, onChange }: ConstraintFieldProps) {
  return (
    <label className="pulse-parameters__field">
      <span>{label}</span>
      <input type="number" value={value} step={step} min={min} disabled={disabled} onChange={(event) => onChange(field, Number(event.target.value))} />
    </label>
  )
}

type NumberFieldProps = {
  field: keyof PulseLabForm
  label: string
  value: number
  error?: string
  step?: number
  min?: number
  max?: number
  disabled?: boolean
  onChange: (field: keyof PulseLabForm, value: number) => void
}

function NumberField({
  field,
  label,
  value,
  error,
  step = 1,
  min,
  max,
  disabled,
  onChange,
}: NumberFieldProps) {
  const errorId = `${String(field)}-environment-error`
  return (
    <label className="pulse-parameters__field">
      <span>{label}</span>
      <input
        type="number"
        value={value}
        step={step}
        min={min}
        max={max}
        disabled={disabled}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        onChange={(event) => onChange(field, Number(event.target.value))}
      />
      {error ? <small className="pulse-parameters__error" id={errorId}>{error}</small> : null}
    </label>
  )
}
