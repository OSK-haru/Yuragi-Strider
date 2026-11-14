---
title: 入力とパラメータ
sidebar_position: 3
---

# 入力とパラメータ

Yuragi-Striderは環境の記述に環境モデル `generic_superconducting_open_system_v3` を用います。入力は、実機で測ることのできる物理量です(0 Kでの $T_1$・$T_\phi$、温度、量子ビット周波数、読み出し共振器の周波数・$\kappa$・$\chi$)。散逸率は、これらから下の式でそのまま決まり、ほかの写像は通りません。

## 入力

| フィールド | 単位 | 制約 | UIの既定値 | UIの入力欄(Gate-aware / Pulse Lab) |
|---|---|---|---|---|
| `t1_zero_temperature_us` | μs | $> 0$ | 40.0 | 「T1(0 K)」/「T1(0 K)[us]」 |
| `tphi_zero_temperature_us` | μs | $> 0$ | 33.0 | 「Tφ(0 K)」/「Tφ(0 K)[us]」 |
| `temperature_mk` | mK | $\ge 0$、有限 | 15.0 | 「温度」/「温度 [mK]」 |
| `qubit_frequency_ghz` | GHz | $> 0$ | 5.0 | 「量子ビット周波数」/「量子ビット周波数 [GHz]」 |
| `readout_resonator_frequency_ghz` | GHz | $> 0$ | 7.0 | 「読み出し共振器の周波数」 |
| `readout_resonator_kappa_mhz` | MHz | $> 0$ | 1.0 | 「共振器の κ/2π」 |
| `readout_resonator_chi_mhz` | MHz | $\ge 0$ | 0.5 | 「分散結合 χ/2π」 |
| `ideal_reference` | — | 真偽値 | false | なし(APIのみ) |

数値はすべて必須です。既定値は見本の値で、特定の実機の校正値ではありません。

- `t1_zero_temperature_us` は、0 Kでのエネルギー緩和時間 $1/\gamma_0$ です。熱浴との結合の強さを表します。有限温度では熱励起が加わるので、実効の $T_1$ はこれより短くなります(下の式)。
- `tphi_zero_temperature_us` は、0 Kでの純粋位相緩和の時間 $1/\gamma_\phi^{(0)}$ です。読み出し共振器の熱光子以外のすべての原因を含み、それらは温度によらないと仮定します($\gamma_0$ と同じ仮定)。有限温度では熱光子による位相緩和が加わるので、実効の $T_\phi$ はこれより短くなります(下の式)。
- `readout_resonator_*` は、量子ビットを読み出す共振器の周波数 $f_r$、線幅 $\kappa/2\pi$、分散結合 $\chi/2\pi$ です。$\chi$ はハミルトニアン $H = \chi\, a^\dagger a\, \sigma_z$ の係数で、量子ビットの状態によって共振器の周波数が $\pm\chi$ ずれます(差は $2\chi$)。符号によらないので大きさを入れます。$\chi = 0$ は共振器と結合していない場合で、熱光子による位相緩和は 0 になります。

量子ビットごとに別の `t1_zero_temperature_us`・`tphi_zero_temperature_us` を与える `per_qubit` もあり、UIでは「量子ビット別校正値」から入力します。入力はAPIのスキーマ(`api/main.py`、`api/pulse_models.py`)で検査された後、`core/physical_environment.py` の `rates_from_times` で次節の散逸率に変換されます。

:::note[入力の意味を変えた項目(2026-11-15)]
以前の `tphi_us` は「動作温度での純粋位相緩和の時間」で、温度を変えても一定でした。これを、0 Kでの値 `tphi_zero_temperature_us` と、温度から導く熱光子の位相緩和に分けました。`tphi_us` を含む要求や設定ファイル(スキーマ2.0)は、読み替えずに拒否します。
:::

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
\gamma_\phi = \gamma_\phi^{(0)} + \Gamma_\phi^{\text{th}},
\qquad
\gamma_\phi^{(0)} = \frac{1}{T_\phi^{(0\,\mathrm{K})}}
$$

$\Gamma_\phi^{\text{th}}$ は、読み出し共振器の熱光子による位相緩和です。共振器の光子数が熱的に揺らぐと、分散結合を通じて量子ビットの周波数が揺らぎ、位相が乱れます。共振器も量子ビットと同じ温度の熱浴にあるとして、

$$
n_r = \frac{1}{\exp\!\left(\dfrac{h f_r}{k_B T}\right) - 1},
\qquad
\Gamma_\phi^{\text{th}} = \frac{\kappa}{2}\,\mathrm{Re}\!\left[\sqrt{\left(1 + \frac{2i\chi}{\kappa}\right)^{2} + \frac{8i\chi n_r}{\kappa}} - 1\right]
$$

です(Clerk & Utami 2007、Rigetti et al. 2012。$\kappa$・$\chi$ は角周波数 $2\pi \times$ MHz)。これは時間 $t \gg 1/\kappa$ での量子ビットのコヒーレンスの減衰率で、一定の率としてLindblad方程式に入れるのは、ほかの率と同じマルコフ近似です。$n_r \ll 1$ では

$$
\Gamma_\phi^{\text{th}} \approx \frac{4\chi^2 \kappa\, n_r}{\kappa^2 + 4\chi^2}
$$

で、$2\chi = \kappa$ のとき $\kappa n_r / 2$ です。既定値($f_r = 7$ GHz、$\kappa/2\pi = 1$ MHz、$\chi/2\pi = 0.5$ MHz)では、15 mKで $\Gamma_\phi^{\text{th}} \approx 6\times10^{-10}$ μs⁻¹(無視できる)、50 mKで約 $4\times10^{-3}$ μs⁻¹(約 260 μs)、120 mKで約 0.20 μs⁻¹(約 5 μs)です。

実装: `core/physical_environment.py` の `thermal_photon_dephasing_rate`。この式は、量子ビットと共振器を一緒に解いたマスター方程式との比較で検証しています([入力モデルの検証](./validations/input.md))。

崩壊演算子は $\sqrt{\gamma_\phi/2}\,\sigma_z$ で、非対角成分は $e^{-\gamma_\phi t}$ で減衰します([散逸モデル](./dissipation-model.md))。

### 実効時間

$$
\gamma_{\text{pop}} = \gamma_\downarrow + \gamma_\uparrow,
\qquad
T_1^{\text{eff}} = \frac{1}{\gamma_{\text{pop}}},
\qquad
T_\phi^{\text{eff}} = \frac{1}{\gamma_\phi},
\qquad
T_2^{\text{eff}} = \frac{1}{\tfrac{1}{2}\gamma_{\text{pop}} + \gamma_\phi}
$$

$T = 0$ では $T_1^{\text{eff}} = T_1^{(0\,\mathrm{K})}$、$T_\phi^{\text{eff}} = T_\phi^{(0\,\mathrm{K})}$ です。

### 実機の測定値から入力を作るとき

温度 $T$ で測った $T_1$・$T_2$ から入力を作るには、次のように換算します($n_{\text{th}}$・$n_r$ は測定時の温度での値)。

$$
\frac{1}{T_1^{(0\,\mathrm{K})}} = \frac{1}{T_1(2n_{\text{th}} + 1)},
\qquad
\frac{1}{T_\phi^{(0\,\mathrm{K})}} = \frac{1}{T_2} - \frac{1}{2T_1} - \Gamma_\phi^{\text{th}}
$$

ここで $T_1$ は測定した(実効の)値です。15 mK程度で測った値なら、$n_{\text{th}}$ も $\Gamma_\phi^{\text{th}}$ も無視でき、測定値をそのまま入れてかまいません。

## 理想参照モード

`ideal_reference: true` を指定すると、すべてのレートが 0、すべての実効時間が無限大になります。ノイズのない理想的な回路動作との比較に用います。無限大の時間は、APIの応答と設定ファイルでは `null` で表します。

## レート変数の命名規約

正準の名称は次のとおりです。

| 変数 | 意味 | UIでの表示(結果パネル) |
|---|---|---|
| `t1_zero_temperature_us` | 入力の $T_1^{(0\,\mathrm{K})}$ | 「T1(0 K)」 |
| `tphi_zero_temperature_us` | 入力の $T_\phi^{(0\,\mathrm{K})}$ | 「Tφ(0 K)」 |
| `gamma_phi_zero_temperature_per_us` | $1/T_\phi^{(0\,\mathrm{K})}$ | なし |
| `gamma_phi_thermal_photon_per_us` | $\Gamma_\phi^{\text{th}}$(熱光子による位相緩和) | なし |
| `n_th_resonator` | 共振器の熱光子数 $n_r$ | なし |
| `gamma0_per_us` | $1/T_1^{(0\,\mathrm{K})}$ | なし |
| `gamma_down_per_us` | $\gamma_\downarrow$(下向き遷移) | 「下降レート」(詳細モード) |
| `gamma_up_per_us` | $\gamma_\uparrow$(熱励起) | 「上昇レート」(詳細モード) |
| `gamma_phi_per_us` | $\gamma_\phi = \gamma_\phi^{(0)} + \Gamma_\phi^{\text{th}}$(純粋位相緩和の合計) | 「純位相緩和レート」(詳細モード) |
| `gamma_population_relaxation_per_us` | $\gamma_\downarrow + \gamma_\uparrow$ | 「占有数緩和レート」(詳細モード) |
| `n_th` | 熱占有数 | なし |
| `t1_effective_us` / `tphi_effective_us` / `t2_effective_us` | 実効時間 | 「実効 T1」「実効 Tφ」「実効 T2」 |

表示の対応は `frontend/src/utils/rateRows.ts` にあります。以前の別名(`gamma1_per_us`、`gammaphi_per_us`、`t1_base_us`、`tphi_base_us`、`t1_us`、`t2_us`)は削除しました。

Pulse Labの「Lindblad率を直接指定」モード(`direct_rates`)では、`gamma_down_per_us`・`gamma_up_per_us`・`gamma_phi_per_us` を「gamma down [1/us]」「gamma up [1/us]」「gamma phi [1/us]」から直接入力でき、上の変換を通りません。

## 検証状況

この入力モデルの検証については[入力モデルの検証](./validations/input.md)を参照してください。熱占有数・詳細釣り合い・有限温度平衡・純粋位相緩和の係数は、独立な解析式との比較で、熱光子による位相緩和の式は、量子ビットと共振器のマスター方程式との比較で検証されています。0 Kでの $T_1$ と $T_\phi$ の値そのものは利用者が与える入力で、モデルの予測ではありません。

温度依存について残っている前提は次のとおりです。

- $\gamma_0$ と $\gamma_\phi^{(0)}$ は温度によらない。準粒子による緩和(およそ 150 mK以上で効き始める)や、磁束ノイズ・電荷ノイズの温度依存は入っていません。
- 共振器は量子ビットと同じ温度の熱浴にある。実機では、配線から入る熱で共振器の実効温度が冷凍機の温度より高いことがよくあります。
- 量子ビットの3準位目(パルスモデルのトランズモン)は、同じ $\gamma_\phi$ を隣接準位間に使います。準位ごとに違う分散シフトは入っていません。
- 熱光子による平均の周波数シフト($\approx 2\chi n_r$)は、入力の量子ビット周波数に含まれているとみなします。
