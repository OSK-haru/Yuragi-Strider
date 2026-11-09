---
title: 散逸モデル
sidebar_position: 5
---

# 散逸モデル

環境との相互作用は、3種類の崩壊演算子(collapse operator)によって表現されます。

## 崩壊演算子

各量子ビットに対して、次の3つが構成されます。

$$
L_\downarrow = \sqrt{\gamma_\downarrow}\,\sigma^-,
\qquad
L_\uparrow = \sqrt{\gamma_\uparrow}\,\sigma^+,
\qquad
L_\phi = \sqrt{\frac{\gamma_\phi}{2}}\,\sigma_z
$$

| 演算子 | 物理過程 | レート |
|---|---|---|
| $L_\downarrow$ | エネルギー緩和(下向き遷移) | $\gamma_\downarrow$ |
| $L_\uparrow$ | 熱励起(上向き遷移) | $\gamma_\uparrow$ |
| $L_\phi$ | 純位相緩和 | $\gamma_\phi$ |

$N$ 量子ビット系では、各演算子が量子ビットごとにテンソル積で展開され、最大 $3N$ 個の崩壊演算子が構成されます。対応するレートが 0 の演算子は生成されません。

実装: `core/gates.py` の `multi_qubit_environment_collapse_operators`(量子ビット別のレートにも対応)。Lindblad生成子への組み込みは `core/cptp_liouvillian.py` です。レートはUIから直接入力せず、[入力とパラメータ](./input_and_parameters.md)の変換で決まります。

## 位相緩和の係数規約

:::warning $\sqrt{\gamma_\phi/2}$ の係数について
位相緩和の崩壊演算子は $\sqrt{\gamma_\phi}\,\sigma_z$ **ではなく** $\sqrt{\gamma_\phi/2}\,\sigma_z$ です。

この規約により、非対角要素の減衰が次式になります。

$$
\rho_{01}(t) = \rho_{01}(0)\, e^{-\gamma_\phi t}
$$

係数を $\sqrt{\gamma_\phi}$ にすると減衰が2倍速くなり、$\gamma_\phi$ の意味が変わってしまいます。この規約の正しさは検証V4で、誤った規約との判別診断(最大誤差 0.119)とともに確認されています。
:::

## 全体の population 緩和レート

$$
\gamma_{\text{pop}} = \gamma_\downarrow + \gamma_\uparrow
$$

:::note
$\gamma_{\text{pop}}$ は**崩壊演算子の係数としては決して使われません**。これは観測される $1/T_1$ に対応する量であり、個別の崩壊演算子は $\gamma_\downarrow$ と $\gamma_\uparrow$ を別々に使います。
:::

## 詳細釣り合い

$\gamma_\downarrow = \gamma_0(1+n_{\text{th}})$、$\gamma_\uparrow = \gamma_0 n_{\text{th}}$ という構成から、詳細釣り合いの関係が自動的に成立します。

$$
\frac{\gamma_\uparrow}{\gamma_\downarrow}
= \frac{n_{\text{th}}}{1+n_{\text{th}}}
= \exp\!\left(-\frac{h f_q}{k_B T}\right)
$$

この結果、系は有限温度のGibbs分布に緩和します。

$$
P_1^{\text{eq}} = \frac{\gamma_\uparrow}{\gamma_\downarrow + \gamma_\uparrow}
$$

検証V2では、この詳細釣り合いが**最大誤差 $5.55\times10^{-17}$** で成立することが確認されています。

## 温度による振る舞い

| 温度 | $n_{\text{th}}$ | 挙動 |
|---|---|---|
| $T = 0$ | 0 | 熱励起なし。$\|1\rangle \to \|0\rangle$ の一方向緩和のみ |
| 低温 | $\ll 1$ | 平衡状態はほぼ $\|0\rangle$ |
| 高温 | $\gg 1$ | 平衡状態は最大混合状態に近づく |

5 GHz の量子ビットにおける参照値は次のとおりです。

```text
   0 mK  →  n_th = 0.0
   1 mK  →  n_th = 6.106056e-105
  10 mK  →  n_th = 3.789449e-11
 100 mK  →  n_th = 9.981031e-2
1000 mK  →  n_th = 3.687302
```

## qutritモデルの散逸

3準位モデル(Pulse Extension B、および3準位を選んだCoupled transmon network)では、遷移ごとに別々のレートを持ちます。


- $0\leftrightarrow1$ 遷移: $\gamma_{10}^{\downarrow}$, $\gamma_{01}^{\uparrow}$
- $1\leftrightarrow2$ 遷移: $\gamma_{21}^{\downarrow}$, $\gamma_{12}^{\uparrow}$

位相緩和は数演算子 $\hat{n}$ を用いた $\sqrt{2\gamma_{\phi,\text{adj}}}\,\hat{n}$ の形をとります(モデル識別子 `number_operator_adjacent_rate_v1`)。この構成により、$\rho_{01}$、$\rho_{12}$、$\rho_{02}$ の減衰比が **1 : 1 : 4** に固定されます。

physicalモードでは $f_{12} = f_{01} + \alpha/1000$、$\gamma_{21}(T{=}0) = 2\gamma_{10}(T{=}0)$ という関係を用います。後者は調和振動子の行列要素に由来する教育用の近似です。

| 量 | 実装(`core/pulse_qutrit_open_system.py`) | UIの入力欄(Pulse Lab) |
|---|---|---|
| physicalモードのレート導出 | `qutrit_dissipation_rates` | 2準位と同じ環境入力と「非調和性 [MHz]」 |
| 直接指定のレート | 同上(`direct_rates`) | 「gamma 10 down」「gamma 01 up」「gamma 21 down」「gamma 12 up」「隣接準位の位相緩和」[1/us] |
| 崩壊演算子 | `qutrit_collapse_operator_matrices` | — |

結合トランズモン網では、同じ崩壊演算子を各トランズモンに付けます(`core/pulse_transmon_network.py` の `network_collapse_operator_matrices`)。

## 検証状況

散逸モデルの各要素は次のように検証されています。

| 項目 | 検証 | 結果 |
|---|---|---|
| 熱占有数・詳細釣り合い | V2 | 最大誤差 5.55e-17、PASS |
| 励起状態の指数減衰 | V3 | 最大誤差 1.99e-08、PASS |
| 純位相緩和の係数規約 | V4 | 最大誤差 9.99e-09、PASS |
| 有限温度平衡 | V5 | 最大誤差 1.95e-11、PASS |
| qutrit遷移別散逸 | B-2 | 詳細釣り合い誤差 5.55e-17、PASS |

詳細は[入力モデルの検証](./validations/input.md)および[時間発展の検証](./validations/propagation.md)を参照してください。
