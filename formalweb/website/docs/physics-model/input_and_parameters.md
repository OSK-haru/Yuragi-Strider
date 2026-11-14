---
title: 入力とパラメータ
sidebar_position: 3
---

# 入力とパラメータ

Yuragi-Striderは環境の記述に環境モデル `generic_superconducting_open_system_v2` を用います。入力は、実機で測ることのできる4つの物理量です。散逸率は、これらから弱結合(Born–Markov)のLindblad方程式の式でそのまま決まり、ほかの写像は通りません。

## 入力

| フィールド | 単位 | 制約 | UIの既定値 | UIの入力欄(Gate-aware / Pulse Lab) |
|---|---|---|---|---|
| `t1_zero_temperature_us` | μs | $> 0$ | 40.0 | 「T1(0 K)」/「T1(0 K)[us]」 |
| `tphi_us` | μs | $> 0$ | 33.0 | 「Tφ」/「Tφ [us]」 |
| `temperature_mk` | mK | $\ge 0$、有限 | 15.0 | 「温度」/「温度 [mK]」 |
| `qubit_frequency_ghz` | GHz | $> 0$ | 5.0 | 「量子ビット周波数」/「量子ビット周波数 [GHz]」 |
| `ideal_reference` | — | 真偽値 | false | なし(APIのみ) |

4つの数値はすべて必須です。既定値は見本の値で、特定の実機の校正値ではありません。

- `t1_zero_temperature_us` は、0 Kでのエネルギー緩和時間 $1/\gamma_0$ です。熱浴との結合の強さを表します。有限温度では熱励起が加わるので、実効の $T_1$ はこれより短くなります(下の式)。
- `tphi_us` は、純粋位相緩和の時間 $1/\gamma_\phi$ です。

量子ビットごとに別の `t1_zero_temperature_us`・`tphi_us` を与える `per_qubit` もあり、UIでは「量子ビット別校正値」から入力します。入力はAPIのスキーマ(`api/main.py`、`api/pulse_models.py`)で検査された後、`core/physical_environment.py` の `rates_from_times` で次節の散逸率に変換されます。

:::note[削除した入力(2026-11-14)]
以前は、0〜1の「デバイス品質」から $T_1$・$T_\phi$ を幾何補間で決める写像、磁束ノイズの振幅から位相緩和率を線形に足す写像(係数 0.05 μs⁻¹)、0〜1の「正規化」入力がありました。どれも文献から導いたものではなく、根拠のない値を物理量のように見せていたため削除しました。これらの項目を含む要求や設定ファイル(スキーマ1.x)は、読み替えずに拒否します。
:::

## レートへの変換

### 温度 → 熱占有数

$$
n_{\text{th}} = \frac{1}{\exp\!\left(\dfrac{h f_q}{k_B T}\right) - 1}
$$

分母には数値安定性のため `expm1` を用いています。また次の安全分岐があります。

- $T \le 0$ または $f_q \le 0$ のとき $n_{\text{th}} = 0$
- 指数部が 700 を超えるとき $n_{\text{th}} = 0$(アンダーフロー回避)

この式は**角周波数 $\omega_q$ ではなく通常の周波数 $f_q$** を用いる規約です($\hbar\omega_q = hf_q$ なので同じ値です)。

実装: `core/physical_environment.py` の `compute_thermal_occupation`(物理定数は `core/constants.py`)。

### 緩和・熱励起レート

$$
\gamma_0 = \frac{1}{T_1^{(0\,\mathrm{K})}},
\qquad
\gamma_\downarrow = \gamma_0 (1 + n_{\text{th}}),
\qquad
\gamma_\uparrow = \gamma_0\, n_{\text{th}}
$$

この構成により、詳細釣り合い $\gamma_\uparrow/\gamma_\downarrow = \exp(-hf_q/k_BT)$ が自動的に満たされます。

### 位相緩和レート

$$
\gamma_\phi = \frac{1}{T_\phi}
$$

崩壊演算子は $\sqrt{\gamma_\phi/2}\,\sigma_z$ で、非対角成分は $e^{-\gamma_\phi t}$ で減衰します([散逸モデル](./dissipation-model.md))。

### 実効時間

$$
\gamma_{\text{pop}} = \gamma_\downarrow + \gamma_\uparrow,
\qquad
T_1^{\text{eff}} = \frac{1}{\gamma_{\text{pop}}},
\qquad
T_2^{\text{eff}} = \frac{1}{\tfrac{1}{2}\gamma_{\text{pop}} + \gamma_\phi}
$$

$T = 0$ では $T_1^{\text{eff}} = T_1^{(0\,\mathrm{K})}$ です。

## 理想参照モード

`ideal_reference: true` を指定すると、すべてのレートが 0、すべての実効時間が無限大になります。ノイズのない理想的な回路動作との比較に用います。無限大の時間は、APIの応答と設定ファイルでは `null` で表します。

## レート変数の命名規約

正準の名称は次のとおりです。

| 変数 | 意味 | UIでの表示(結果パネル) |
|---|---|---|
| `t1_zero_temperature_us` | 入力の $T_1^{(0\,\mathrm{K})}$ | 「T1(0 K)」 |
| `tphi_us` | 入力の $T_\phi$ | 「Tφ」 |
| `gamma0_per_us` | $1/T_1^{(0\,\mathrm{K})}$ | なし |
| `gamma_down_per_us` | $\gamma_\downarrow$(下向き遷移) | 「下降レート」(詳細モード) |
| `gamma_up_per_us` | $\gamma_\uparrow$(熱励起) | 「上昇レート」(詳細モード) |
| `gamma_phi_per_us` | $\gamma_\phi$(純粋位相緩和) | 「純位相緩和レート」(詳細モード) |
| `gamma_population_relaxation_per_us` | $\gamma_\downarrow + \gamma_\uparrow$ | 「占有数緩和レート」(詳細モード) |
| `n_th` | 熱占有数 | なし |
| `t1_effective_us` / `t2_effective_us` | 実効時間 | 「実効 T1」「実効 T2」 |

表示の対応は `frontend/src/utils/rateRows.ts` にあります。以前の別名(`gamma1_per_us`、`gammaphi_per_us`、`t1_base_us`、`tphi_base_us`、`t1_us`、`t2_us`)は削除しました。

Pulse Labの「Lindblad率を直接指定」モード(`direct_rates`)では、`gamma_down_per_us`・`gamma_up_per_us`・`gamma_phi_per_us` を「gamma down [1/us]」「gamma up [1/us]」「gamma phi [1/us]」から直接入力でき、上の変換を通りません。

## 検証状況

この入力モデルの検証については[入力モデルの検証](./validations/input.md)を参照してください。熱占有数・詳細釣り合い・有限温度平衡・純粋位相緩和の係数は、独立な解析式との比較で検証されています。$T_1$ と $T_\phi$ の値そのものは利用者が与える入力で、モデルの予測ではありません。
