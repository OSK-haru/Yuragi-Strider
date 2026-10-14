/*
 * アルゴリズムのプリセット。IDE画面のナビゲーターに並べる。
 */
import type { SectionIconName } from '../components/SectionIcon'
import type { CircuitPresetKey } from '../context/CircuitContextCore'

export type AlgorithmPreset = {
  key: CircuitPresetKey
  icon: SectionIconName
  category: string
  title: string
  qubits: number
  difficulty: string
  description: string
  highlights: string[]
}

export const algorithmPresets: AlgorithmPreset[] = [
  {
    key: 'grover_2qubit',
    icon: 'chart',
    category: '探索アルゴリズム',
    title: 'Grover探索（2量子ビット）',
    qubits: 2,
    difficulty: '入門〜中級',
    description:
      '4通りの状態からCZゲートのオラクルで|11>だけに位相を反転させ、1回の拡散変換（振幅増幅）で測定確率を高めます。2量子ビットなら1回の反復で最適化されるため、Groverアルゴリズムの仕組みを最短で確認できます。',
    highlights: ['振幅増幅', 'オラクル', '拡散変換'],
  },
  {
    key: 'grover_4qubit',
    icon: 'chart',
    category: '探索アルゴリズム',
    title: 'Grover探索（4量子ビット）',
    qubits: 4,
    difficulty: '中級',
    description:
      '16通りの状態から|0100>を探索します。位相オラクルの後に、H⁴・X⁴・多重制御X・X⁴・H⁴で構成した拡散変換を3回実行し、解の振幅を増幅します。制御点を追加した多重制御Xが、探索回路でどう働くかを確認できます。',
    highlights: ['16状態', '3回反復', '多重制御X', '拡散変換'],
  },
  {
    key: 'teleportation',
    icon: 'atom',
    category: '量子通信プロトコル',
    title: '量子テレポーテーション',
    qubits: 3,
    difficulty: '入門',
    description:
      'エンタングルメントと2ビットの古典通信を使い、量子状態をq0からq2へ転送します。ベル対の生成・ベル測定・条件付き補正という量子通信の基本パターンを確認できます。',
    highlights: ['エンタングルメント', 'ベル測定', '古典条件付きゲート'],
  },
  {
    key: 'magic_state',
    icon: 'atom',
    category: '量子誤り訂正',
    title: 'マジック状態 |T> の生成と劣化',
    qubits: 2,
    difficulty: '入門〜中級',
    description:
      'q0にH・Tを適用してマジック状態 |T> = (|0> + e^{iπ/4}|1>)/√2 を作ります。この状態はCliffordゲートだけでは到達できない向き（Bloch球の八面体の外）を向いており、誤り訂正計算機ではTゲート1回ごとにこれを1個消費します。参照用のq1にはHのみを掛けて|+>（Cliffordで到達できる八面体の頂点）を置いてあります。Bloch球上で両者を見比べると、|T>だけが頂点から45°ずれた「八面体の外」を向いていることが分かります。シミュレーション時間を延ばすと、両方のBlochベクトルが環境ノイズで原点に向かって縮み、劣化したマジック状態が使えなくなる様子が見えます。',
    highlights: ['Tゲート', 'Clifford+T', 'Bloch球', 'デコヒーレンス'],
  },
  {
    key: 'bit_flip_repetition',
    icon: 'circuit',
    category: '量子誤り訂正',
    title: '3量子ビット反復符号',
    qubits: 5,
    difficulty: '中級',
    description:
      '1つの論理量子ビットを3つの物理量子ビットに符号化します。q1にX（ビット反転）故障を注入し、2つの補助量子ビットでシンドロームを測定して、条件付きゲートで自動的に誤りを訂正します。',
    highlights: ['誤り訂正符号', 'シンドローム測定', '条件付き補正'],
  },
]
