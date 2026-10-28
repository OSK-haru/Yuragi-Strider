export type ModelLabelStatus = 'current' | 'default' | 'preview' | 'legacy' | 'experimental'

export type ModelLabelInfo = {
  id: string
  label: string
  description: string
  status: ModelLabelStatus
  statusLabel: string
}

/*
 * ここの ID はバックエンドの定数と 1 対 1 で対応させる。
 * 変更するときは core/capabilities.py・core/backend_boundary.py・
 * core/gate_aware_cptp.py の該当定数と必ず突き合わせること。
 */
export const MODEL_IDS = {
  simulationModel: 'gate_aware_open_system',
  evolutionMode: 'gate_aware_hamiltonian_lindblad_v1',
  splitStepMode: 'gate_aware_split_step_v1',
  cptpEvolution: 'gate_aware_constant_gksl_exponential_v1',
  weakCouplingModel: 'weak_coupling_lindblad',
  postCircuitModel: 'post_circuit_degradation_v1',
  defaultBackend: 'python_dense',
  previewBackend: 'rust_dense_preview',
  pythonDenseBackendName: 'python_dense_streaming_v1',
  effectiveUnitaryHamiltonian: 'effective_unitary_spectral_generator_v2',
  nativeGateSet: 'gate_aware_hxyzst_rz_cnot_v3',
} as const

export const modelLabels: Record<string, ModelLabelInfo> = {
  [MODEL_IDS.simulationModel]: {
    id: MODEL_IDS.simulationModel,
    label: 'ゲートを考慮した開放系',
    description:
      '理想的な瞬時ゲートではなく、開放量子系として回路を発展させます。',
    status: 'current',
    statusLabel: '現在のモデル',
  },
  [MODEL_IDS.evolutionMode]: {
    id: MODEL_IDS.evolutionMode,
    label: 'ゲートを考慮したハミルトニアン Lindblad v1',
    description:
      '各ゲート列を有効ハミルトニアンで表現し、操作中に Lindblad ノイズを作用させます。',
    status: 'current',
    statusLabel: '現在の発展方式',
  },
  [MODEL_IDS.splitStepMode]: {
    id: MODEL_IDS.splitStepMode,
    label: 'ゲートを考慮したスプリットステップ v1',
    description:
      'ハミルトニアン Lindblad と同じ実行経路に解決される互換用のモデル ID です。',
    status: 'legacy',
    statusLabel: '互換用の別名',
  },
  [MODEL_IDS.cptpEvolution]: {
    id: MODEL_IDS.cptpEvolution,
    label: '定数 GKSL 指数発展（CPTP）',
    description:
      '各区間で GKSL 生成子を一定とみなし、指数写像で CPTP 発展させる経路です。',
    status: 'experimental',
    statusLabel: '実験的な発展方式',
  },
  [MODEL_IDS.weakCouplingModel]: {
    id: MODEL_IDS.weakCouplingModel,
    label: '弱結合 Lindblad',
    description:
      'ゲート時間を陽に扱わない、弱結合近似での基準 Lindblad モデルです。',
    status: 'legacy',
    statusLabel: '基準モデル',
  },
  [MODEL_IDS.postCircuitModel]: {
    id: MODEL_IDS.postCircuitModel,
    label: '回路実行後の減衰 v1',
    description:
      '回路の実行後にのみ環境ノイズによる減衰を適用する簡易モデルです。',
    status: 'legacy',
    statusLabel: '簡易モデル',
  },
  [MODEL_IDS.defaultBackend]: {
    id: MODEL_IDS.defaultBackend,
    label: 'Python 密行列バックエンド',
    description:
      '小規模な密度行列シミュレーション用の標準参照バックエンドです。',
    status: 'default',
    statusLabel: 'デフォルトバックエンド',
  },
  [MODEL_IDS.previewBackend]: {
    id: MODEL_IDS.previewBackend,
    label: 'Rust 密行列プレビュー',
    description:
      '任意で使用できる高速化プレビュー経路です。検証済みの標準バックエンドではありません。',
    status: 'preview',
    statusLabel: 'プレビューバックエンド',
  },
  [MODEL_IDS.pythonDenseBackendName]: {
    id: MODEL_IDS.pythonDenseBackendName,
    label: 'Python 密行列ストリーミング v1',
    description:
      '時系列を逐次書き出しながら計算する、Python 密行列バックエンドの実装名です。',
    status: 'default',
    statusLabel: '実装名',
  },
  [MODEL_IDS.effectiveUnitaryHamiltonian]: {
    id: MODEL_IDS.effectiveUnitaryHamiltonian,
    label: '有効ユニタリのスペクトル生成子 v2',
    description:
      'ゲートの目標ユニタリを対角化し、その対数から有効ハミルトニアンを構成します。',
    status: 'current',
    statusLabel: 'ハミルトニアン構成',
  },
  [MODEL_IDS.nativeGateSet]: {
    id: MODEL_IDS.nativeGateSet,
    label: 'ネイティブゲート集合 HXYZST/RZ/CNOT v3',
    description:
      'コンパイラが回路を分解する先の、ハードウェア相当のゲート集合です。',
    status: 'current',
    statusLabel: 'ゲート集合',
  },
}

export function getModelLabel(id: string): ModelLabelInfo {
  return (
    modelLabels[id] ?? {
      id,
      label: id,
      description: 'この内部 ID に対応する表示名はまだ登録されていません。',
      status: 'current',
      statusLabel: '内部 ID',
    }
  )
}
