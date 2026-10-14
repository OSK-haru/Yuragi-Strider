# Reset漏洩課題の利用者学習校正

実施日: 2026-09-29
状態: **training予算・instance preflightとも承認・内部実装済み**

## 1. 目的

従来の正規推定器は物理modelから導いた最良閾値を使っていた。これは内部受入には使えるが、攻撃者の体験を検証しない。
そこで、攻撃者へ返されたラベル付きtraining結果の`known_label`、`shots`、`counts`だけから頻度閾値推定器を構成し、hidden評価へ渡す経路を実装した。

実装:

- `core/attack_simulation/reset_leakage_analysis.py`
- `scripts/calibrate_reset_leakage_training.py`
- `tests/test_reset_leakage_analysis.py`

内部真値、Reset成否、条件付き確率、hidden bit、測定seedは推定器の入力にしない。

## 2. 学習方法

label 0と1について、許可された回数だけ32 shotsのtraining queryを実行する。
各labelの全countsを集約して観測1の頻度を求め、その中点を閾値とする。観測方向が逆なら判定方向も反転する。
両labelの頻度が同一で分離不能な場合は、model値へfallbackせず学習失敗とする。

## 3. 統計校正

Reset課題専用v0の条件付き観測確率
`P(1|v=0)=0.03692`、`P(1|v=1)=0.13092`を使い、trainingとhidden 20件の有限sampleを反復した。
hidden合格条件は承認済みの20件中16件である。

| labelごとのtraining query | training総query | training総shots | 反復 | 合格率 | 学習不能率 | 方向反転率 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 64 | 10,000 | 54.55% | 6.90% | 4.56% |
| 2 | 4 | 128 | 10,000 | 66.74% | 2.04% | 1.67% |
| 4 | 8 | 256 | 10,000 | 74.56% | 0.19% | 0.11% |
| 8 | 16 | 512 | 10,000 | 78.17% | 0.00% | 0.00% |
| 10 | 20 | 640 | 10,000 | 78.16% | 0.00% | 0.00% |
| 16 | 32 | 1,024 | 3,000 | 79.80% | 0.00% | 0.00% |
| 32 | 64 | 2,048 | 3,000 | 81.50% | 0.00% | 0.00% |

model由来の最良閾値でもhidden合格率は理論82.29%である。trainingを増やしても、この上限へ近づくだけで、確率的な不合格は消えない。

## 4. 実経路の確認

labelごと8 query、各32 shotsの公開training結果から推定器を構築し、同じorchestratorとhidden graderへ渡す固定seedのend-to-end testを追加した。
preflight追加後、Reset関連の焦点test 82件は成功した。

これは公開Application API、疑似terminal、未信頼code sandboxを通す製品E2Eではない。Reset固有の内部経路について、model由来閾値を利用者学習へ混入せず完走できることの証拠である。

## 5. 残る製品判断

### D-RT1: training予算（採用済み）

統計上の必要量を**labelごと8 query**とし、余剰2 queryを加えた**labelごと10 query、合計20 query・640 shots**をReset課題v0へ採用した。
8回と10回はいずれも10,000反復で学習不能・方向反転が0件だった。10回の合格率78.16%は8回の78.17%と同等であり、追加分を性能向上ではなく有限sampleの余裕として扱う。

`ResetLeakageV0Settings.training_queries_per_label = 10`、`training_query_limit = 20`として実装した。これは他課題や共通基盤の既定値にはしない。

### D-RT2: 確率的hidden instance（方針採用・内部実装済み）

承認済みの20件中16件条件では、正規推定器にも偶然の不合格がある。
課題modeでは、instance生成時に非公開seed bundleを固定し、公開trainingだけから作るcanonical estimatorが合格し、定数推定器と対照条件が不合格になるinstanceだけを発行する。
利用者へseedやcase別結果は公開しない。閾値を下げる方式や、判定を繰り返して結果を選ぶ方式より、既存判定条件と観測予算を崩しにくい。

自由modeと研究的評価ではseedを選別せず、無作為または利用者指定とする。課題modeと同じ物理model・runtimeを使い、違いはinstance生成policyだけに限定する。
preflight済み課題は攻略可能性を確認したCTF instanceであり、その合格率を無作為instance上の攻撃成功率として報告しない。自由・研究用途の無選別実行を統計評価に使う。

seed bundleはtraining測定seed、hidden case順seed、hidden測定seed、未知被害者query用secret seedを持つ。`preflight_reset_leakage_instance()`は最大試行回数内で候補を探索し、選定policy、seed bundle、training集計、三条件のhidden結果をmanager-private reportへ残す。適格instanceが得られなければ課題生成を失敗させる。

固定した選定seedによる内部確認では一回目の候補が通過し、canonical estimatorは16/20、perfect Resetとisolated resourceは各10/20、定数推定器は各10/20だった。seed値自体は通常利用者向け結果へ投影しない。永続manifestへの格納は共通基盤の永続化責務として未実装である。

## 6. 再現方法

```powershell
python scripts/calibrate_reset_leakage_training.py --training-queries-per-label 1 2 4 8 --repetitions 10000 --seed 20260929 --generated-at-utc 2026-09-29T00:00:00+00:00
```

16・32 query/labelの行は同じscriptを3,000反復で実行した参考値である。
