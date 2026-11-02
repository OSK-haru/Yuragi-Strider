# Gate-aware 測定モデル

## 実装範囲

Gate-aware の `MEASURE` は、計算基底での射影測定として状態に作用する。
通常の `MEASURE` は結果を読み捨てる**非選択測定**として扱い、古典ビットを指定した
`MEASURE` は選択測定 instrument として分岐へ保存する。ポストセレクションは未対応。
読み出し割当誤りは測定状態へ作用させず、最終観測確率にだけ適用する。

これとは別に、シミュレーション終了時の全量子ビットの確率分布から、
指定した shots 数だけ再現可能な標本を生成し、状態ごとの counts を返す。

## 一般式

量子ビット $q$ を計算基底で測定する射影演算子を

$$
P_m^{(q)} = |m\rangle\langle m|_q \otimes I_{\bar q},
\qquad m\in\{0,1\}
$$

とする。個々の測定結果を保持する場合、結果 $m$ の確率と条件付き状態は

$$
p(m)=\operatorname{Tr}\!\left(P_m^{(q)}\rho\right),
\qquad
\rho_m=\frac{P_m^{(q)}\rho P_m^{(q)}}{p(m)}
$$

である。現在の実装では結果を読み捨てるため、確率で平均した状態

$$
\mathcal M_q(\rho)
=\sum_{m=0}^{1}p(m)\rho_m
=\sum_{m=0}^{1}P_m^{(q)}\rho P_m^{(q)}
$$

を使う。この写像は Kraus 演算子 $\{P_0^{(q)},P_1^{(q)}\}$ を持つ CPTP
チャネルである。複数量子ビットを同じ列で測定する場合は、それらの
同時計算基底射影を用いる。

## 実装時の変形と理由

上式を密度行列の各要素へ展開すると、測定対象ビットについて行添字と
列添字のビット値が異なる成分だけが 0 になる。すなわち、対象集合を $Q$
として

$$
[\mathcal M_Q(\rho)]_{ij}
=
\begin{cases}
\rho_{ij}, & i_q=j_q\quad(\forall q\in Q),\\
0, & \text{otherwise}.
\end{cases}
$$

コードではこの要素ごとの形を使う。射影行列の積を毎回構成しないための
計算上の変形であり、近似ではない。確率（対角成分）と未測定部分の許される
コヒーレンスを保持し、測定対象にまたがるコヒーレンスだけを除去する。

同じ回路列に測定と別量子ビット上のゲートがある場合、両者は異なる部分系に
作用して可換であるため、列のユニタリ作用後に測定チャネルを適用する。
回路検証は同じ量子ビットを同一列の複数演算で共有する配置を許可しない。

## 最終 shots

最終密度行列の計算基底確率は

$$
p_x=\langle x|\rho_{\mathrm{final}}|x\rangle
$$

である。shots 数を $N$ とすると、返却する counts は

$$
(n_x)_x\sim\operatorname{Multinomial}\!\left(N,(p_x)_x\right),
\qquad \sum_x n_x=N
$$

に従う。実装は累積分布の逆変換サンプリングを独立に $N$ 回行う。
`measurement_seed` で乱数系列を固定するため、同じ確率分布・shots・seed なら
同じ counts を再現できる。量子ビット表記は既存規約どおり `q0` を最上位
ビットとする。

## 理想状態との忠実度

非選択測定後の理想状態は一般に混合状態となる。純粋状態専用の
$\operatorname{Tr}(\rho\sigma)$ をそのまま使うと、同一の混合状態同士でも
忠実度が純度まで低下してしまう。そのため、理想状態 $\sigma$ が混合状態の
場合は Uhlmann 忠実度

$$
F(\rho,\sigma)=
\left[\operatorname{Tr}\sqrt{\sqrt{\sigma}\rho\sqrt{\sigma}}\right]^2
$$

を使用する。$\sigma$ が純粋なら既存式
$F(\rho,\sigma)=\operatorname{Tr}(\rho\sigma)$ に簡約して計算する。
ノイズなし理想経路にも同じ測定チャネルを適用するので、タイムライン、
Bloch 球、密度行列の比較は測定後の正しい理想状態を基準にする。

## UI/API 契約

- API 入力: `measurement_options.shots`, `measurement_options.seed`
- API 出力: `measurement.counts`, `measurement.frequencies`
- 明示的 `MEASURE`: `non_selective_computational_basis_v1`
- 最終標本化: `final_computational_basis_shots_v1`
- 測定直後の状態スナップショット種別: `measurement`
- `classical_bits`、`classical_targets`、条件式の回路スキーマを v2 として保持
- v2 の選択測定 instrument と古典レジスタ分岐を実装
- 条件付きゲートは最大4096分岐までを上限として実行
- 分岐ごとにGate-awareのゲート区間ノイズ（RK4 / Explicit CPTP）を適用
- 最終読出し割当 `binary_readout_assignment_v1` はRustで一括適用可能
- 未対応: ポストセレクション、分岐の確率的統合

`MEASURE` はユニタリ行列ではなくチャネルである。したがって回路列の
ユニタリを組み立てる段階では恒等作用として扱い、各列の完了境界で専用の
測定チャネルを適用する。この区別により RK4 と Explicit CPTP、および
Python と Rust のいずれの時間発展経路でも同じ測定意味論を保つ。

条件付きゲートを含む回路は、`gate_aware_noisy_branching_v1` として測定結果ごとに
密度行列を分岐させ、各ゲートの実効ハミルトニアンと同じ環境崩壊演算子で発展させる。
`fixed_step_rk4` と `explicit_cptp` の両方に対応し、Rust指定時はCPTP経路のRust
カーネルを再利用する。ただし5量子ビット条件付き回路では、32次元密度行列のChoi監査が
過大になるため、メイン経路と分岐経路をRK4へ明示的にフォールバックする。分岐数が上限を
超える場合は暗黙に近似せず、明示的な実行エラーとして返す。API/UIの
`classical_branching_noise_applied` と診断モードで経路を開示する。

### 重要プリセット

- `teleportation`: 3量子ビット、2回の測定、2ビットAND条件によるX/Z補正
- `bit_flip_repetition`: 5量子ビット（3データ+2シンドローム）、既知のX故障を注入し、
  2ビットシンドロームで位置別補正

これらは、理想環境では既知の入力状態を復元し、有限ノイズでは復元確率が低下することを
数値テストで確認するための監査用ケースでもある。
