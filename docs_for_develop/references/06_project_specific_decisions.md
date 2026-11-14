# 文献から直接は決まらない設計判断

## 目的

物理・数値文献は設計の重要な根拠になるが、Yuragi-Striderの全仕様が論文から
一意に導かれるわけではない。この文書は、外部文献の権威へ過剰に帰属しては
ならない判断を明示する。

## 1. Gate-aware有効Hamiltonian

Yuragi-Striderでは、`U^2 = I`を満たす対象gateについて、

$$
H_{\mathrm{gate}}
=
\frac{\pi}{2\tau}(I-U)
$$

を使用し、

$$
\exp(-iH_{\mathrm{gate}}\tau)=U
$$

となるようにする。

### 区分

`PROJECT DECISION`

### 根拠

これは行列指数から直接確認できる代数的構成である。GKSL文献は、この
Hamiltonianと散逸を同じgeneratorへ入れる一般形式を支えるが、この具体式を
Yuragi-Striderへ採用すること自体はプロジェクト判断である。

### 使用箇所

- `core/gates.py`
- `core/simulator.py`
- `tests/test_gate_aware_hamiltonian_lindblad.py`
- `docs/physics/model_identity.md`

### 制限

- 任意角回転へそのまま一般化しない。
- 実機pulse Hamiltonianを再現するものではない。
- global phaseの扱いをcontract内で固定する必要がある。

## 2. Gate duration defaults

```text
H:       0.02 us
X:       0.02 us
Z:       0.0 us
CNOT:    0.20 us
MEASURE: 0.0 us
```

### 区分

`PROJECT DECISION`

これらは教育用presetのdefaultであり、特定hardwareの校正値ではない。
ユーザー編集値またはgate固有`duration_us`が優先される。

## 3. 環境の入力（2026-11-14に変更、D89）

### 区分

`MODEL BASIS`（弱結合のGKSL）。入力の値そのものは利用者が与える。

入力は、実機で測れる量だけにした: 0 KでのT1（`t1_zero_temperature_us`、$1/\gamma_0$）、
純粋位相緩和の時間（D90で`tphi_zero_temperature_us`、0 Kでの$1/\gamma_\phi^{(0)}$に変更）、温度、量子ビット周波数、
（D90から）読み出し共振器の周波数・$\kappa$・$\chi$。$\gamma_\phi=\gamma_\phi^{(0)}+\Gamma_\phi^\mathrm{th}$で、
$\Gamma_\phi^\mathrm{th}$は共振器の熱光子による位相緩和（Clerk & Utami 2007）。
$\gamma_\downarrow=\gamma_0(n_\mathrm{th}+1)$、$\gamma_\uparrow=\gamma_0 n_\mathrm{th}$、
$1/T_1=\gamma_\downarrow+\gamma_\uparrow$。

### 削除したもの

- `device_quality`（0-1）からT1・Tφへの幾何補間（1〜100 µs）
- 磁束ノイズの振幅から$\gamma_\phi$への線形写像（係数0.05 µs⁻¹）
- 0-1の「正規化」入力（温度10〜100 mKへの線形写像など）

どれも文献から導いたものではなく、物理量の単位を持つ入力のように見えて根拠がなかったため、
注意書きを付けて残すのではなく削除した。磁束ノイズを入れ直すなら、周波数可変transmonの
磁束感度と1/fノイズのスペクトルから導く別の機能として作る。

### 使用箇所

- `core/physical_environment.py`
- `core/results.py`（`EnvironmentConfig`）
- `frontend/src/components/ParameterPanel.tsx`

## 4. Pure-dephasing規約

### 区分

`PROJECT DECISION` + `VALIDATED CONVENTION`

Yuragi-Striderは、

$$
L_\phi=\sqrt{\gamma_\phi/2}\sigma_z
$$

を採用し、隣接coherenceの減衰率を`gamma_phi`とする。

文献によって`gamma_phi`という記号の定義が異なるため、平方根内の係数だけを
比較して実装の正誤を判定しない。master equationへ代入したoff-diagonal
decay rateで比較する。

### 検証

- `scripts/validate_pure_dephasing.py`
- `docs/validation/validation-4-pure-dephasing.md`
- `validation_results/validation4_pure_dephasing.json`

## 5. Qutrit number dephasing

### 区分

`PROJECT DECISION` + `VALIDATED CONVENTION`

number operatorを用いたdephasingでは、level差の二乗に比例してcoherence decay
が変わる規約を採用する。隣接coherenceと`|0><2|` coherenceの`1:4`関係を
検証fixtureで確認する。

### 使用箇所

- `core/pulse_qutrit_open_system.py`
- `scripts/validate_pulse_qutrit_dissipation.py`
- `docs/validation/pulse-b-qutrit-dissipation.md`

## 6. 数値toleranceとstep policy

### 区分

`PROJECT DECISION` + `VALIDATION RESULT`

次は一般論文から一意に決まらない。

- RK4 step cap
- qutrit work budget
- Choi CP/TP tolerance `1e-12`
- QuTiP `atol=rtol=1e-12`
- case-specific trace-distance acceptance limit
- timeout

これらはdimension、Hamiltonian norm、rate、実行環境、目的精度を考慮し、
事前登録したrefinement testとstress testで管理する。

### 使用箇所

- `core/pulse_step_policy.py`
- `core/cptp.py`
- `validation_cptp/qutip_audit.py`
- `api/pulse_models.py`

## 7. Density-matrix cleanup

### 区分

`PROJECT DECISION`

RK4 pathではcomplete step後にtrace/Hermiticity cleanupを行うが、
raw diagnosticsとcleanup correction normを保存する。PSD projectionで
粗いstepの不安定性を隠さない。

Explicit CPTP pathではstate cleanupを使用しない。

### 使用箇所

- `core/simulator.py`
- `core/pulse_evolution.py`
- `core/cptp_piecewise.py`
- `docs/validation/cptp-rk4-comparison.md`

## 8. Choi配列規約

### 区分

`PROJECT DECISION`

Choi theoremは完全正値性の判定を支えるが、次は実装間比較のための固定規約で
ある。

```text
unnormalized_input_output_row_major_v1
column_major_vec_f_v1
```

異なる正規化・tensor orderのChoi matrixを、要素やtraceだけで直接比較しない。

## 9. Model IDsとclaim boundary

### 区分

`PROJECT DECISION`

```text
driven_two_level_rwa_experimental_v1
driven_transmon_qutrit_rwa_experimental_v1
yuragi_strider_explicit_cptp_v1
```

`experimental`は、数理・数値検証済みであってもhardware-calibrated modelでは
ないことを示す。QuTiP一致だけを理由に`validated hardware model`へ改名しない。

## 10. 文献追加時のチェック

新しい論文を追加するときは、次を確認する。

1. 書誌情報とDOIが正しい。
2. 原著論文、review、software paperの区別がある。
3. 実装した内容と、背景として読んだだけの内容を分離している。
4. 使用箇所が具体的なfileまたはvalidation artifactへ結びついている。
5. 文献が支えない範囲を記載している。
6. 実機妥当性をsolver一致から推論していない。
