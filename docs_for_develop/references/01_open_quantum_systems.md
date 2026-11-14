# 開放量子系・散逸モデル

## 対象

この文書は、密度行列、Markov型量子力学的半群、GKSL/Lindblad方程式、
有限温度の上下遷移、緩和と純粋位相緩和の位置づけを扱う。

## 1. Gorini-Kossakowski-Sudarshan

**区分:** `FOUNDATIONAL`

V. Gorini, A. Kossakowski, and E. C. G. Sudarshan,
"Completely positive dynamical semigroups of N-level systems,"
*Journal of Mathematical Physics* 17, 821-825 (1976).
[DOI: 10.1063/1.522979](https://doi.org/10.1063/1.522979)

### 文献の内容

有限次元量子系における完全正値な力学的半群のgeneratorの一般形を与える。
現在GKSL generatorと呼ばれる構造の原典の一つである。

### Yuragi-Striderで使用した内容

- Hamiltonian交換子と散逸子を同じmaster equationで扱う。
- 非負rateを持つcollapse operatorからMarkov型generatorを構成する。
- 時間一定generatorの指数写像をCPTP mapとして扱う。

### 使用箇所

- `core/gates.py`
- `core/simulator.py`
- `core/pulse_open_system.py`
- `core/pulse_qutrit_open_system.py`
- `core/cptp_liouvillian.py`
- `docs/physics/model_identity.md`

### この文献だけでは支えないもの

- Born-Markov近似がYuragi-Striderの全入力範囲で成立すること
- 使用rateが特定実機のrateと一致すること
- 固定step RK4の有限step写像がCPTPであること

## 2. Lindblad

**区分:** `FOUNDATIONAL`

G. Lindblad, "On the generators of quantum dynamical semigroups,"
*Communications in Mathematical Physics* 48, 119-130 (1976).
[DOI: 10.1007/BF01608499](https://doi.org/10.1007/BF01608499)

### 文献の内容

量子力学的半群のgeneratorを作用素論的に特徴づける。GKS論文と合わせて、
現在のGKSL/Lindblad形式の基礎を与える。

### Yuragi-Striderで使用した内容

$$
\frac{d\rho}{dt}
=
-i[H,\rho]
+
\sum_k
\left(
L_k\rho L_k^\dagger
-
\frac{1}{2}
\{L_k^\dagger L_k,\rho\}
\right)
$$

をgate-aware、two-level Pulse、qutrit Pulseの共通散逸形式として使用した。

### 使用箇所

- `core/simulator.py`
- `core/pulse_evolution.py`
- `core/cptp_liouvillian.py`
- `validation_results/validation3_excited_state_decay.json`
- `validation_results/validation4_pure_dephasing.json`
- `validation_results/validation5_finite_temperature_equilibrium.json`

### この文献だけでは支えないもの

- Yuragi-Striderのrate mapping
- `gamma_phi`の係数規約
- qutritを三準位で打ち切る精度
- gate-aware有効Hamiltonianの具体式

## 3. Clerk et al.

**区分:** `MODEL BASIS`

A. A. Clerk, M. H. Devoret, S. M. Girvin, F. Marquardt, and R. J. Schoelkopf,
"Introduction to quantum noise, measurement, and amplification,"
*Reviews of Modern Physics* 82, 1155-1208 (2010).
[DOI: 10.1103/RevModPhys.82.1155](https://doi.org/10.1103/RevModPhys.82.1155)

### 文献の内容

量子雑音spectral density、有限温度bath、詳細釣り合い、量子測定を含む
量子雑音のレビューである。

### Yuragi-Striderで使用した内容

- ボソン熱浴の平均占有数

$$
\bar n_{\mathrm{th}}
=
\frac{1}{\exp(\hbar\omega/k_{\mathrm B}T)-1}
$$

- 有限温度で下向き・上向き遷移を分ける考え方
- 上下rateの比をthermal detailed balanceと整合させる考え方

### 使用箇所

- `core/physical_environment.py`
- `core/pulse_qutrit_open_system.py`
- `scripts/validate_zero_temperature_thermal_excitation.py`
- `scripts/validate_finite_temperature_equilibrium.py`

### この文献だけでは支えないもの

- 利用者が与えるT1・Tφの値（2026-11-14に、品質と磁束ノイズからの写像は削除した。D89）
- 実機の非平衡bathや周波数依存spectral density

## 4. Ithier et al.

**区分:** `MODEL BASIS`

G. Ithier et al., "Decoherence in a superconducting quantum bit circuit,"
*Physical Review B* 72, 134519 (2005).
[DOI: 10.1103/PhysRevB.72.134519](https://doi.org/10.1103/PhysRevB.72.134519)

### 文献の内容

超伝導qubitのdecoherenceをnoise spectral density、relaxation、
dephasing、およびNMR由来の測定手法と結びつけて解析する。

### Yuragi-Striderで使用した内容

- population relaxationとphase decoherenceを分離して表示する考え方
- `T1`、`Tphi`、`T2`を区別する説明方針
- Ramsey等を将来のhardware audit observableに含める方針

### 使用箇所

- `core/physical_environment.py`
- `core/expert_data.py`
- `docs/physics/監査方針/validation8_real_hardware_observable_validation_plan.md`
- `docs/requirements/yuragi_strider_physical_model_finalization_plan.md`

### この文献だけでは支えないもの

- Yuragi-Striderのpure-dephasing collapse operator係数を一意に決めること
- Quantroniumの校正値をtransmon一般へ転用すること
- Markov型の定数rateが全時間scaleで妥当であること

## 実装規約との対応

Yuragi-Striderでは、

$$
L_\phi
=
\sqrt{\frac{\gamma_\phi}{2}}\sigma_z
$$

と定義し、off-diagonal coherenceが
`exp(-gamma_phi * t)`で減衰する規約を採用する。この係数は文献名だけから
自動的に決まるものではなく、`gamma_phi`を何の減衰率と定義するかに依存する。
Yuragi-Striderの規約はV4で解析解に対して検証した。

有限温度のpopulation relaxation timeは、

$$
\frac{1}{T_1}
=
\gamma_\downarrow+\gamma_\uparrow
$$

と定義する。`gamma_down`単独ではなく、平衡値へのpopulation差の減衰率として
上下rateの和を用いる。
