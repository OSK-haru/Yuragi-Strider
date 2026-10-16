# Reset漏洩課題の初回校正

実施日: 2026-09-28
状態: **Reset課題専用v0 profileとして承認済み**

## 1. 校正範囲

`PhenomenologicalResetModel`の宣言済み挙動に対し、次を比較した。

- Reset成功確率: `0.80`, `0.90`, `0.95`, `0.98`
- 一caseのshot数: `8`, `16`, `32`, `64`
- hidden評価: 0と1を各10件、合計20件
- 合格候補: 20件中14〜18件正解
- 負の対照: perfect Reset、isolated physical resource

固定した探索parameterは次のとおりである。

- Reset成功時の励起人口: `0.02`
- `P(observed=1 | true=0) = 0.02`
- `P(observed=0 | true=1) = 0.04`
- Reset回数: `1`

これらは初回の難易度校正用parameterであり、論文値、特定実機の校正値、ハードウェア再現値ではない。

## 2. 比較結果

合格条件を20件中16件正解とした場合の主要候補を示す。通過率は、候補profileから導いた最良のcount閾値推定器に対する厳密確率である。

| 候補 | Reset成功確率 | shots/case | 相互情報量 [bit/case] | 推定精度 | 正規推定器の通過率 | perfect Reset誤通過率 | resource分離誤通過率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 明瞭 | 0.80 | 16 | 0.6246 | 0.8974 | 95.26% | 0.016% | 0.007% |
| 標準候補 | 0.90 | 32 | 0.4880 | 0.8476 | 82.29% | 0.0156% | 0.0097% |
| 微弱 | 0.95 | 64 | 0.3515 | 0.7896 | 58.39% | 0.161% | 0.136% |

`0.98`では64 shotでも推定精度0.6353、通過率8.47%であり、現行の20件中16件条件には適さない。

## 3. 標準候補（一部承認）

2026-09-28に、所有者はこの数値群をReset課題専用のv0 profileとして承認した。これは挙動再現用の製品設定であり、実機のReset fidelityや全課題共通の既定値を表さない。`ResetLeakageV0Settings.scope = reset_leakage_challenge_only`としてコード上でも適用範囲を明記する。

標準候補の条件付き観測確率は次のとおりである。

- `P(attacker=1 | victim=0) = 0.03692`
- `P(attacker=1 | victim=1) = 0.13092`
- count閾値: 32 shot中3回以上の1を観測した場合にvictim=1と推定
- Bayes最適精度: `0.8476435`
- 相互情報量: `0.4879974 bit/case`
- 攻撃者優位性: `0.3476435`
- model configuration ID: `sha256:47b34a4a4261150ef347495e904530688a7c1e6adf54324e4d093f2206b77028`

受入監査へ投入した暫定閾値は次のとおりで、全項目を通過した。

- 候補の最低相互情報量: `0.45 bit/case`
- 候補の最低攻撃者優位性: `0.30`
- 各負の対照の最大相互情報量・優位性: `1e-9`
- hidden評価: 20件中16件正解

この閾値は今回の決定論的な挙動校正に対する候補である。有限sampleから相互情報量を推定する将来の計測へ、そのまま流用してはならない。

## 4. 内部経路benchmark

標準候補を内部hidden graderで100回実行した。

- 通過: `78 / 100`
- 平均正解数: `16.86 / 20`
- 実測通過率: `78%`
- 理論通過率: `82.29%`
- 一評価の平均時間: `0.852 s`
- 中央値: `0.838 s`
- p95（nearest rank）: `0.953 s`
- 最大: `1.056 s`

実測通過率は100反復だけの確認値であり、理論値を置き換えるものではない。実行時間は現在のホストと内部実装に依存し、ブラウザ、通信、未信頼code sandboxを含まない。

## 5. 再現方法

統計校正:

```powershell
python scripts/calibrate_reset_leakage_policy.py --success-probabilities 0.80 0.90 0.95 0.98 --target-excited-population 0.02 --observed-one-given-zero 0.02 --observed-zero-given-one 0.04 --shots 8 16 32 64 --case-count 20 --zero-case-count 10 --required-correct 14 15 16 17 18 --generated-at-utc 2026-09-28T00:00:00+00:00
```

内部grader benchmark（2026-10-16: 対象のorchestratorと`scripts/benchmark_reset_leakage_grader.py`は削除済み。以下は当時の記録）:

```powershell
python scripts/benchmark_reset_leakage_grader.py --success-probability 0.90 --target-excited-population 0.02 --observed-one-given-zero 0.02 --observed-zero-given-one 0.04 --shots-per-case 32 --case-count 20 --zero-case-count 10 --required-correct 16 --repetitions 100 --seed 1000 --generated-at-utc 2026-09-28T00:00:00+00:00
```

## 6. 承認前に残る判断

所有者は次を一組のprofileとして承認または差し戻す。

1. **承認済み:** Reset成功確率`0.90`を初期課題用の挙動parameterとして採用する。
2. **承認済み:** 標準の攻撃者観測を`32 shots/case`とする。preset生成時に変更可能とし、解決値をmanifestへ記録する。
3. **承認済み:** hidden評価を0/1各10件の20件とし、16件以上の正解を合格とする。
4. **承認済み:** 最低相互情報量`0.45 bit/case`、最低攻撃者優位性`0.30`、負の対照上限`1e-9`を決定論的校正の受入閾値とする。

なお、現在の正規推定器はmodel-derived閾値を用いる。ユーザーがラベル付き学習だけから同等の閾値を得られるかという体験上の校正は、別途必要である。
