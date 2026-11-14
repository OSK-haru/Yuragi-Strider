---
title: 物理モデルの概要
sidebar_position: 1
---

# 物理モデルの概要

Yuragi-Striderでは、ユーザーが設定した量子回路と、0 Kでの $T_1$・$T_\phi$・温度・量子ビット周波数という環境パラメータから、制御項と散逸項を構成します。

制御モデルとして **Gate-awareモデル** または **Pulse-levelモデル** を選択し、各時間区間のハミルトニアンを決定します。さらに環境パラメータから緩和・熱励起・位相緩和のレートと崩壊演算子を導出し、Lindblad型(GKSL型)の時間発展方程式を構成します。

この方程式を **RK4による数値積分**、**明示的CPTP写像による時間発展**、または理想条件下での**状態ベクトル発展**のいずれかで計算し、密度行列、Fidelity、Purity、出力確率などを可視化します。

## 計算の流れ

入力から出力までは、次の5段で構成されます。

```text
[1] 入力          量子回路 (H, CNOT, Rz, 測定 …)
                  環境パラメータ (T, S_Φ(ω), ω_q/2π, T₁, T₂ …)
                        │
[2] モデル構成    制御モデル  Gate-aware または Pulse-level  →  H(t)
                  散逸モデル  緩和 T₁ / 熱励起 n_th / 位相緩和 T_φ  →  {L_k}
                        │
[3] 方程式        H(t) と {L_k} からGKSL方程式を構成
                        │
[4] 時間発展      RK4 / CPTP写像 / 状態ベクトル のいずれかで積分
                        │
[5] 出力          ρ(t) → Fidelity, Purity, 出力確率, Bloch球
```

第3段で構成される方程式が、このシミュレーターの中心にあります。

$$
\frac{d\rho}{dt} = -i\left[H(t), \rho\right] + \sum_k \mathcal{D}[L_k]\rho
$$

第1項が制御による可逆な発展を、第2項が環境との相互作用による不可逆な散逸を表します。散逸子 $\mathcal{D}$ の定義と各項の導出は[Lindblad方程式](./lindblad.md)を参照してください。

## モデルの構成

| 段階 | 内容 | 詳細 |
|---|---|---|
| 入力 | 環境パラメータ → 物理レート | [入力とパラメータ](./input_and_parameters.md) |
| 制御 | 回路 → 時間区間ごとのハミルトニアン | [Gate-aware](./control_models/gate-awaremodel.md) / [Pulse-level](./control_models/pulse-levelmodel.md) |
| 散逸 | レート → 崩壊演算子 | [散逸モデル](./dissipation-model.md) |
| 方程式 | GKSL方程式の構成 | [Lindblad方程式](./lindblad.md) |
| 時間発展 | 数値解法の選択 | [RK4](./propagation/RK4.md) / [CPTP](./propagation/CPTP.md) / [状態ベクトル](./propagation/statevector.md) |
| 出力 | 指標の算出 | [出力](./outputs.md) |

:::tip[理論から学びたい場合]
このセクションは「実装が何をしているか」の仕様書です。同じ物理を定義と導出から順に学びたい場合は、[理論を学ぶ](../learn/index.md)を参照してください。上の表の各段階に、1つずつ章が対応しています。
:::

## 現在のモデル識別子

実装が宣言している既定のモデル識別子は次のとおりです。

```text
evolution_mode:   gate_aware_hamiltonian_lindblad_v1
environment:      generic_superconducting_open_system_v1
hamiltonian_mode: effective_unitary_spectral_generator_v2
native_gate_set:  gate_aware_hxyzst_rz_cnot_v3
```

## 適用範囲について

Yuragi-Striderは**校正済みのハードウェアモデルではありません**。実装しているデバイスプロファイルは一般的なトランズモンを模した学習用のものであり、特定の実機を定量的に再現することを目的としていません。

前提と適用範囲の詳細は[前提と適用範囲](./assumuptions.md)を、各モデルがどのように検証されたかは[妥当性検証](./validations/index.md)を参照してください。
