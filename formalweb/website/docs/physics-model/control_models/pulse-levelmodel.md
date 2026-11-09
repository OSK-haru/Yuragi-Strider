---
title: Pulse-levelモデル
sidebar_position: 2
---

# Pulse-levelモデル

実際の超伝導量子コンピューターでは、量子回路をトランズモンと呼ばれる人工原子に照射するマイクロ波パルスとして実行します。論理的な「ゲート」という部品は存在せず、パルスの波形そのものが操作の実体です。

Pulse-levelモデルは、ゲートを論理的な単位としてではなく、**時間依存の制御パルス**として扱います。パルスの包絡線、位相、離調を直接指定し、その駆動下での状態発展を計算します。

Gate-awareモデルより低いレイヤーを扱うため、漏れ(leakage)、DRAG補正、パルス形状の影響といった、論理ゲートでは表現できない現象を観察できます。

:::info[Pulse LabとCircuit Studioの関係]
トランズモン1台のときは選択レーンを順次実行します。2台以上ではPulse Circuit Studioの全レーンを1本の時間軸へまとめ、同時駆動を含めて1回のAPI要求で実行します。Gate-awareの論理回路とは状態を共有しません。
:::

## 共通の前提

3つのパルスモデルはすべて次を共有します。

```text
frame          : rotating(回転系)
approximation  : RWA(回転波近似)
hardware_calibrated : false
experimental   : true
units          : μs / rad·μs⁻¹ / μs⁻¹
```

離調は「駆動周波数 − 量子ビット周波数」として定義されます。

$$
\Delta = \omega_d - \omega_{01}
$$

## 3つのモデル

| | Baseline A | Extension B | Coupled network |
|---|---|---|---|
| model_id | `driven_two_level_rwa_experimental_v1` | `driven_transmon_qutrit_rwa_experimental_v1` | `driven_coupled_transmon_network_rwa_experimental_v1` |
| contract | `pulse-baseline-a-v1` | `pulse-extension-b-v1` | `pulse-transmon-network-v1` |
| 準位数 | 2 | 3 | $L^N$、$L\in\{2,3\}$、$1\le N\le4$ |
| 状態 | available | available | **experimental** |
| DRAG | 不可(β = 0 強制) | Gaussianのみ | Gaussianのみ・スケジュール別 |
| 準静的ノイズ | 非対応 | 対応(次数 3/5/7/9) | 対応(次数 3/5、隣接相関あり) |
| 発展方式 | RK4 / 明示的CPTP | RK4 / 明示的CPTP | RK4 / 明示的CPTP(次元 ≤ 9) |
| 作業上限 | 200,000 ステップ | 25,000 ステップ | 次元依存のdense work 1,200,000,000 / 500 CPTP区間 |

いずれも同一のエンドポイント `POST /api/pulse/simulate` から `model_id` による判別で利用します。

ネットワークモデルは局所準位数 `local_levels`(2または3)と台数 `transmon_count`(1〜4)の2軸で構成されます。$N = 1$ は交換結合を持たない縮退ケースで、Pulse LabのUIはこの2軸だけを提示します。

:::note[旧「結合ペア」モデルについて]
`driven_coupled_transmon_pair_rwa_experimental_v1`(contract `pulse-coupled-pair-v1`)は廃止されました。同じ構成は **ネットワークモデルの `local_levels = 3`, `transmon_count = 2`** で表現できます。CPTP経路と相関準静的ノイズもネットワーク側へ移植済みです。
:::

## 包絡線と振幅

### 包絡線の形状

| 形状 | 指定 | UIの入力欄 | 実装(`core/pulse_envelopes.py`) |
|---|---|---|---|
| `square` | `pulse_duration_us` | 「Pulse長 [us]」 | `SquarePulseEnvelope` |
| `gaussian` | `sigma_us` と `truncation_sigma` | 「Gaussian σ [us]」「打ち切り [σ]」 | `GaussianPulseEnvelope` |

Gaussianでは所要時間が次式で導出されるため、`pulse_duration_us` の指定は拒否されます。

$$
\tau_p = 2 N_{\text{trunc}}\, \sigma
$$

有限区間で打ち切ったGaussianは、無限台のGaussianではなく**打ち切り後の面積**で正規化されます。この扱いにより、打ち切り 3σ でも回転角の誤差が 0 になります(無限台の正規化を使うと $8.5\times10^{-3}$ の誤差が生じます)。

### 振幅モード

排他的に1つだけ指定します。

| モード | 必要なフィールド | UIの入力欄 |
|---|---|---|
| `target_rotation_angle` | `target_rotation_angle_rad` | 「回転角 [rad]」 |
| `peak_amplitude` | `peak_amplitude_rad_per_us` | 「ピーク振幅 [rad/us]」 |

### パラメータとUI・実装の対応

| 量 | 記号 | 単位 | UIの入力欄(Pulse Lab) | 実装 |
|---|---|---|---|---|
| 2準位の駆動ハミルトニアン | $H=\tfrac{\Omega}{2}(\cos\phi\,\sigma_x+\sin\phi\,\sigma_y)+\tfrac{\Delta}{2}\sigma_z$ | rad/μs | — | `core/pulse_envelopes.py` の `TwoLevelPulseHamiltonian` |
| 3準位の駆動ハミルトニアン | 対角 $(0,-\Delta,-2\Delta+\alpha)$、遷移 $\tfrac{\Omega}{2}$ と $\sqrt2\,\tfrac{\Omega}{2}$ | rad/μs | — | `core/pulse_qutrit_contract.py` の `qutrit_rotating_frame_hamiltonian` |
| 駆動の位相 | $\phi$ | rad | 「位相 [rad]」 | 各Pulseの `phase_rad` |
| 離調 | $\Delta$ | rad/μs | 「離調 [rad/us]」 | `core/pulse_contract.py` の `detuning_rad_per_us` |
| DRAG係数 | $\beta$ | μs | 「DRAG β [us]」 | `core/pulse_transmon_network.py` の `ScheduledTransmonDrive.quadratures` ほか |
| 非調和性 | $\alpha$ | MHz | 「非調和性 [MHz]」 | `core/pulse_qutrit_contract.py` |
| 各トランズモンの基準離調 | $\Delta_i$ | rad/μs | 「q0 基準離調 [rad/us]」「q1 基準離調 [rad/us]」 | `core/pulse_transmon_network.py` の `CoupledTransmonNetworkHamiltonian` |
| 交換結合 | $J$ | **rad/μs**(文献の $J/2\pi$ [MHz] とは $2\pi$ 倍異なる) | 「隣接交換結合 J [rad/us]」 | 同上(`TransmonExchangeCoupling`) |
| トランズモンの周波数 | $f_i$ | GHz | 「量子ビット周波数 [GHz]」(全トランズモン共通) | 散逸率の計算に使用 |
| 準静的な離調ノイズ | $\sigma_\Delta$ | rad/μs | 「離調の標準偏差 σ [rad/us]」、ネットワークは「共通 σ [rad/us]」「隣接相関係数 r」 | `core/quasi_static_noise.py` |
| 単位換算 | MHz・GHz → rad/μs | — | — | `core/pulse_contract.py` の `mhz_to_rad_per_us`、`ghz_to_rad_per_us` |

## Pulse Baseline A(2準位)

最も基本的なモデルです。駆動された2準位系を回転系・RWAで扱います。

散逸は $\sqrt{\gamma_\downarrow}\sigma^-$、$\sqrt{\gamma_\uparrow}\sigma^+$、$\sqrt{\gamma_\phi/2}\sigma_z$ の3つです。パルス中の散逸と、パルス後の待機区間の両方が計算されます。

環境入力は `physical` と `direct_rates` の2モードです。

ステップ方針 `pulse_baseline_a_step_policy_v1`:

$$
h \cdot G_H \le 0.05, \qquad h \cdot G_D \le 0.05, \qquad \frac{h}{\sigma} \le \frac{1}{20}
$$

**制限**: 2準位のみで漏れを表現しない、RWAのみ、マルコフ的Lindbladのみ、DRAG・伝達関数・クロストーク・多量子ビットパルス制御は非対応。

## Pulse Extension B(qutrit)

第3準位を含めることで、漏れとDRAG補正を扱えます。

Duffing型のトランズモンとして、非調和性 $\alpha$ を持ちます。

$$
\alpha_{[\mathrm{rad/\mu s}]} = 2\pi\, \alpha_{[\mathrm{MHz}]},
\qquad
f_{12} = f_{01} + \frac{\alpha}{1000}
$$

散逸は遷移ごとに指定でき、位相緩和は数演算子形式 $\sqrt{2\gamma_{\phi,\text{adj}}}\,\hat{n}$ を用います。これにより $\rho_{01} : \rho_{12} : \rho_{02}$ の減衰比が 1 : 1 : 4 に固定されます。

### DRAG制御

Gaussian包絡線に対して、微分成分を直交クアドラチャに加えることで漏れを抑制します。

$$
\Omega_y(t) = -\beta \frac{d\Omega_x(t)}{dt}
$$

検証B-4での効果($\beta = 0.001\ \mu\mathrm{s}$、π パルス):

| 指標 | β = 0 | β = 0.001 |
|---|---|---|
| パルス終端の漏れ | 0.260634 | **0.022695** |
| 目標忠実度 | 0.647631 | **0.936293** |

ステップ方針 `qutrit_fixed_rk4_v1` は Baseline A より厳しく、$\varepsilon_H = \varepsilon_D = 0.02$、σ あたり 32 サンプルです。

**制限**: 単一qutritのみ、3準位切り詰め、RWAのみ、準静的Gaussian離調が唯一の非マルコフ的ノイズ、**固定ステップRK4は厳密な有限ステップCPTP積分ではない**。

## Coupled transmon network(1〜4トランズモン)

Pulse Labの主モデルです。各トランズモンを $L$ 準位($L = 2$ または $3$)で切り、基底順はq0を最上位とする辞書順です。$N = 1$ は交換結合を持たない縮退ケース、$N \ge 2$ で結合が入ります。ハミルトニアンは

$$
H(t)=\sum_i H_i(t)+\sum_{(i,j)\in E}J_{ij}
\left(a_i^\dagger a_j+a_i a_j^\dagger\right)
$$

です。APIの `couplings` は任意の辺集合を、`drives` は `target`・`start_time_us`・局所Pulseを指定します。重なった時間帯のドライブは同時に加算されます。Pulseごとの離調は、各局所回転フレーム内の位相ランプとして適用されます。

Pulse Circuit Studioから実行すると、各レーンは時刻0から始まり、レーン内のdriveはPulse間隔を挟んで直列、異なるレーン同士は並列に配置されます。Virtual Zは時間を進めず、そのレーンの後続Pulse位相へ加算されます。UIは現在、隣接レーンへ共通の $J$ を設定しますが、API自体は辺ごとに異なる $J_{ij}$ を受け付けます。

密度行列の次元は $L^N$、要素数は $L^{2N}$ です。実行前に次の2つを検査します。

- `estimated_steps × ((L^N)^3 + 12,000)` ≤ 1,200,000,000 の次元依存作業量
- `sample_count × (L^N)^2 ≤ 250,000` の応答行列要素数

作業量の式にある固定項12,000は、1ステップあたりの密行列演算以外の準備コストを同じ単位で数えたものです。これにより同じ予算が台数によらず実行時間を抑えます。3準位4台($L^N = 81$)では内部ステップ約2,200回・スナップショット38点までで、16 nsのGaussianパルスを各レーン1〜2個並べる規模が入ります。2準位を選べば同じ台数でも次元が $2^N$ に下がるため、より長い時間発展が入ります。

RK4経路の積分はNumPy密行列カーネルで行います（`backend` の python/rust 指定は他モデル向けで、RK4のネットワーク応答は `numpy_dense` を返します）。ジャンプ演算子は各トランズモンの局所構造を使って適用するため、台数が増えても散逸項の費用が密行列積のようには膨らみません。

**明示的CPTP写像**は Hilbert次元が9以下のとき(3準位×最大2台、2準位×最大3台)に選べます。区間ごとに監査つきGKSL指数を合成するため、次元が上がるとChoi行列が $(L^N)^2 \times (L^N)^2$ に膨らむことからこの上限を設けています。CPTPの区間数上限は500です。

2準位を選んだ場合、局所演算子はqutritの演算子を量子ビット部分空間へ切り出したものになります。$\lvert 2\rangle$ が存在しないため $1 \leftrightarrow 2$ の遷移は消え、数演算子は $\mathrm{diag}(0,1)$ になります。非調和性は入力として必須ですが(ステップ方針の決定に使われます)、量子ビット部分空間の発展には寄与しません。

クロストーク、伝達関数、可変結合器ダイナミクスは未対応です。物理入力モードでは、`frequencies_ghz` の各周波数からトランズモンごとの散逸率を計算します。直接レート入力は全トランズモンで同じ値を使います。

## 準静的ノイズ

ショットごとに固定され、1ショット内では変化しないGaussian離調のゆらぎを扱います。

$$
H(t;\delta) = H_0(t) - (\Delta + \delta)\,\hat{n},
\qquad
\delta \sim \mathcal{N}(0, \sigma_\omega^2)
$$

モンテカルロサンプリングではなく、**決定論的なGauss-Hermite求積**で評価されます(次数 3/5/7/9)。

ネットワークモデルでは、Cholesky分解による**相関付き多変量版**が使えます(次数 3/5)。各トランズモンに独立した $\sigma_i$ を与え、隣接ペアに共通の相関係数 $r$ を掛けた三重対角の共分散

$$
\Sigma_{ij} = \sigma_i \sigma_j \cdot
\begin{cases}
1 & i = j \\
r & |i-j| = 1 \\
0 & \text{otherwise}
\end{cases}
$$

から、次数$^N$ 本の軌道を重み付き平均します。$\sigma_i = 0$ のトランズモンはその軸を畳み、鎖の相関を断ちます。

純度はアンサンブル平均後の密度行列から $\operatorname{Tr}(\bar\rho^2)$ として計算されます(各サンプルの純度の平均ではありません)。

## 実行時の制約

APIは同時に最大2つのパルスジョブを実行します。

| ステータス | 意味 |
|---|---|
| 422 | スキーマ・タイミング・モード・作業量の拒否 |
| 503 | 実行スロットが両方とも使用中 |
| 504 | 90秒の待機タイムアウトを超過 |

作業量が上限を超える要求は、数値計算の**実行前**に 422 で拒否されます。

## 検証状況

単一トランズモンの2モデルはQuTiP 5.2.3との独立比較を通過しています。

| モデル | ケース数 | 許容誤差 | 最大誤差 | 判定 |
|---|---|---|---|---|
| Baseline A | 6 | $5\times10^{-7}$ | $6.61\times10^{-8}$ | PASS |
| Extension B | 8 | $5\times10^{-7}$ | $5.03\times10^{-10}$ | PASS WITH RESTRICTIONS |

ネットワークモデルの検証状況は次のとおりです。

| 対象 | 状況 |
|---|---|
| 3準位・2〜4台(RK4) | QuTiPとの独立比較(6ケース)。テンソル基底、Hermiticity、交換結合の励起数保存も回帰試験で確認 |
| 2準位・1〜4台 | QuTiPとの独立比較(3ケース)。QuTiP側は `destroy(2)` から真の2次元演算子を組むため、production の**スライス実装が独立に検証**されています |
| 相関準静的ノイズ | QuTiPとの独立比較(3ケース、$N=2$と$N=3$の鎖、ゼロ幅による鎖の切断を含む)。QuTiP側は共分散とCholesky分解を契約から組み直します |
| 明示的CPTP(次元 ≤ 9) | QuTiPとの独立比較(3ケース、2準位・3準位の両方)。Choi監査(`all_maps_cptp`)とRK4との一致も回帰試験で確認 |

独立比較は合計 **15ケース・150チェックポイント、全PASS** です(`validation_results/pulse_transmon_network_qutip_audit.json`)。RK4経路の最大要素誤差は $2.28\times10^{-9}$、CPTP経路は $2.07\times10^{-5}$ です。

:::note[CPTPの許容誤差がRK4より緩い理由]
明示的CPTPは区間ごとにハミルトニアンを中点で凍結した指数写像を合成します。GKSL指数は無条件安定なので大きな区間でも発散しませんが、**安定性は精度ではありません** — 区間が包絡線を分解できなければ、正しく積分された「別のパルス」になります。誤差は区間幅の2乗で落ちるため、区間幅はRK4の精度上限に係数3を掛けた値に固定してあります。これは行列指数の本数を1桁減らしつつ、QuTiP比較を $2\times10^{-5}$ に収めるための設計上の折り合いです。
:::

詳細は[制御モデルの検証](../validations/control-models.md)を参照してください。

:::note[旧「結合ペア」モデルの検証記録について]
廃止された `pulse-coupled-pair-v1` はQuTiPとの独立比較(7ケース・93チェックポイント、最大誤差 $1.51\times10^{-7}$)を通過していましたが、モデルの削除に伴い監査成果物も取り下げました。同じ物理構成(3準位×2台)はネットワークモデルで再現でき、**その構成を含む独立比較は現行の監査で実施済み**です。
:::
