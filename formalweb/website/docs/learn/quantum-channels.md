---
title: 8. 量子チャネルとCPTP
sidebar_position: 9
---

# 8. 量子チャネルとCPTP

:::info[このページで学ぶこと]
- 時間発展を「微分方程式」ではなく「写像」として見る立場
- 物理的な状態変化に要求される4つの条件
- なぜ正値性では足りず、**完全**正値性が必要なのか(転置写像の反例)
- Kraus表現とChoi行列、CPTP性を数値的に検証する方法
- Liouvillian超演算子とベクトル化 — 行列指数で厳密に解く道
- この経路の計算コストが $4^n\times4^n$ になる理由

**対応する処理レイヤー**: 時間発展(明示的CPTP経路)
:::

## 8.1 視点の転換

第7章までは「$\rho$ が微分方程式に従って連続的に動く」という見方をしてきました。本章では見方を変えます。

$$
\mathcal{E}: \rho \ \longmapsto\ \rho'
$$

**始点の状態を終点の状態に移す写像**として、時間発展をひとまとめに捉えます。この立場をとると次の問いが立てられます。

> 物理的に許される写像 $\mathcal{E}$ とは、どういうものか?

答えは、GKSL方程式が「あの形しかありえない」理由そのものです。

## 8.2 量子チャネルの4条件

写像 $\mathcal{E}$ が物理的な状態変化を表すには、次を満たす必要があります。

**(1) 線形性**
$\mathcal{E}(p\rho_1 + (1-p)\rho_2) = p\,\mathcal{E}(\rho_1) + (1-p)\,\mathcal{E}(\rho_2)$

「コインを投げて $\rho_1$ か $\rho_2$ を用意してから操作する」ことと「それぞれに操作してから混ぜる」ことが一致しなければなりません。

**(2) エルミート性の保存**
$\rho = \rho^\dagger \Rightarrow \mathcal{E}(\rho) = \mathcal{E}(\rho)^\dagger$

**(3) トレース保存(TP)**
$\operatorname{Tr}\mathcal{E}(\rho) = \operatorname{Tr}\rho = 1$

確率の総和は 1 であり続けます。

**(4) 完全正値性(CP)**
任意の次元 $d$ について、$\mathcal{E}\otimes I_d$ が正値写像であること。

(1)〜(3) は自然な要求ですが、(4) には説明が必要です。

## 8.3 なぜ「完全」正値性か

素朴には「正値性」、つまり $\rho \succeq 0 \Rightarrow \mathcal{E}(\rho) \succeq 0$ を要求すれば足りそうに見えます。密度行列を密度行列に移せばよい、と。

**しかしこれでは不十分です。** 理由は、**操作する量子ビットが、操作しない別の量子ビットとエンタングルしているかもしれない**からです。

系Aに操作 $\mathcal{E}$ を掛けるとき、実際に起きるのは $\mathcal{E}\otimes I_B$ です。$\mathcal{E}$ 単体が正値でも、$\mathcal{E}\otimes I_B$ が正値とは限りません。

### 転置写像という反例

転置 $\mathcal{T}(\rho) = \rho^{\mathsf{T}}$ を考えます。転置は固有値を変えないので、$\mathcal{T}$ は明らかに正値でトレース保存です。1量子ビットだけを見ている限り、何の問題もありません。

ところがBell状態 $|\Phi^+\rangle = \frac{1}{\sqrt2}(|00\rangle+|11\rangle)$ に部分転置 $\mathcal{T}\otimes I$ を作用させると

$$
\rho_{\Phi^+} = \frac{1}{2}\begin{pmatrix}
1&0&0&1\\0&0&0&0\\0&0&0&0\\1&0&0&1
\end{pmatrix}
\ \xrightarrow{\ \mathcal{T}\otimes I\ }\
\frac{1}{2}\begin{pmatrix}
1&0&0&0\\0&0&1&0\\0&1&0&0\\0&0&0&1
\end{pmatrix}
$$

右辺の固有値は $\frac{1}{2},\frac{1}{2},\frac{1}{2},-\frac{1}{2}$ で、**負の固有値が現れます**。転置は正値だが完全正値ではありません。

:::tip[CP性は「他の量子ビットへの配慮」]
完全正値性とは、「操作していない量子ビットとエンタングルしていても矛盾が起きない」ことの保証です。量子ビットが1つしかない世界なら正値性で十分ですが、量子計算機では常に他の量子ビットが存在します。

シミュレーターが1量子ビットずつ独立に散逸を適用する場合でも、CP性は必要です。エンタングルした多量子ビット状態に対して、その適用が意味を持つために必要だからです。
:::

なお、部分転置で負の固有値が出るかどうかはエンタングルメントの判定基準(PPT判定)としても使われます。この例は偶然ではなく、CP性とエンタングルメントが深く結びついていることの表れです。

## 8.4 Kraus表現

CPTP写像には、驚くほど単純な一般形があります。

$$
\boxed{\
\mathcal{E}(\rho) = \sum_i K_i\,\rho\,K_i^\dagger,
\qquad
\sum_i K_i^\dagger K_i = I
\ }
$$

$K_i$ を**Kraus演算子**といいます。この形の写像は必ずCPTPであり、逆にすべてのCPTP写像はこの形に書けます(Kraus 1971)。

条件 $\sum_i K_i^\dagger K_i = I$ がトレース保存に対応します。

$$
\operatorname{Tr}\mathcal{E}(\rho) = \sum_i\operatorname{Tr}(K_i\rho K_i^\dagger) = \operatorname{Tr}\!\left(\Big[\sum_i K_i^\dagger K_i\Big]\rho\right) = \operatorname{Tr}\rho
$$

**例(ユニタリ)**: $K_1 = U$ のみ。$U^\dagger U = I$ を満たします。ユニタリ発展はKraus演算子が1つのCPTP写像です。

**例(振幅減衰)**: $p = 1 - e^{-\gamma t}$ として

$$
K_0 = \begin{pmatrix}1 & 0\\ 0 & \sqrt{1-p}\end{pmatrix},
\qquad
K_1 = \begin{pmatrix}0 & \sqrt{p}\\ 0 & 0\end{pmatrix}
$$

$K_1$ が「量子を1つ環境へ放出した」場合、$K_0$ が「放出しなかった」場合に対応します。第3章のジャンプ項/no-jump項の構造がそのまま現れています。実際、GKSL方程式を微小時間 $dt$ 積分すると

$$
K_0 = I - \left(iH + \tfrac{1}{2}\textstyle\sum_k L_k^\dagger L_k\right)dt,
\qquad
K_k = \sqrt{dt}\,L_k
$$

となり、**GKSL方程式はKraus表現の微小時間版**であることが分かります。

## 8.5 Choi行列 — CP性を数値で検証する

写像 $\mathcal{E}$ が完全正値かどうかを、実際にどう確かめればよいでしょうか。任意の次元 $d$ について確認するのは不可能に見えます。

答えは**Choi–Jamiołkowski同型**が与えてくれます。最大エンタングル状態を1つ用意し、その半分にだけ $\mathcal{E}$ を作用させます。

$$
J(\mathcal{E}) = \big(\mathcal{I}\otimes\mathcal{E}\big)\Big(\sum_{i,j}|ii\rangle\langle jj|\Big)
= \sum_{i,j} |i\rangle\langle j| \otimes \mathcal{E}\big(|i\rangle\langle j|\big)
$$

この $d^2\times d^2$ 行列を**Choi行列**といいます。

> **定理**: $\mathcal{E}$ が完全正値 ⟺ $J(\mathcal{E}) \succeq 0$
>
> $\mathcal{E}$ がトレース保存 ⟺ $\operatorname{Tr}_{\text{out}}J(\mathcal{E}) = I$

**1つの行列の固有値を調べるだけで、すべての次元に対する完全正値性が判定できます。** これが Choi 同型の威力です。

Yuragi-Striderの明示的CPTP経路は、各区間の写像についてこの監査を実行します。

| 性質 | 判定 | 許容誤差 |
|---|---|---|
| 完全正値性(CP) | $J$ の最小固有値 $\ge -\epsilon$ | $10^{-12}$ |
| トレース保存性(TP) | 部分トレースが恒等演算子 | $10^{-12}$ |

:::note[許容誤差が必要な理由]
理論上は最小固有値がちょうど 0 以上のはずですが、浮動小数点演算では $-10^{-16}$ 程度の値が出ます。$10^{-12}$ という閾値は、丸め誤差は許すが実質的な破れは検出する、という水準に設定されています。
:::

## 8.6 Liouvillianとベクトル化

写像の話を、実際に計算する形に落とします。

$\mathcal{L}$ は行列 $\rho$ に作用する線形写像なので、$\rho$ を縦に伸ばして**ベクトル**とみなせば、$\mathcal{L}$ は普通の行列になります。この操作を**ベクトル化**といいます。

$$
\operatorname{vec}\begin{pmatrix}a & c\\ b & d\end{pmatrix}
= \begin{pmatrix}a\\ b\\ c\\ d\end{pmatrix}
\qquad(\text{列優先})
$$

鍵になる恒等式は次のものです。

$$
\operatorname{vec}(A\,X\,B) = \left(B^{\mathsf{T}}\otimes A\right)\operatorname{vec}(X)
$$

これを使うとGKSL生成子が $d^2\times d^2$ の行列として書けます。

$$
\mathcal{L} = -i\left(I\otimes H - H^{\mathsf{T}}\otimes I\right)
+ \sum_k\left[
\bar{L}_k\otimes L_k
- \frac{1}{2}\Big(I\otimes L_k^\dagger L_k + (L_k^\dagger L_k)^{\mathsf{T}}\otimes I\Big)
\right]
$$

($\bar{L}$ は複素共役。$(L^\dagger)^{\mathsf{T}} = \bar{L}$ を使っています。)

これで区間の発展は、ただの行列指数の掛け算になります。

$$
\operatorname{vec}\big(\rho(t+\Delta t)\big) = \exp\!\left(\mathcal{L}\,\Delta t\right)\operatorname{vec}\big(\rho(t)\big)
$$

:::warning[ベクトル化の規約は必ず確認する]
列優先(column-major)か行優先(row-major)かで、$\otimes$ の左右が入れ替わります。Yuragi-Striderは `column_major_vec_f_v1` を宣言しています。Choi行列にも規約があり(`unnormalized_input_output_row_major_v1`、添字順は input ⊗ output、$\operatorname{Tr}J = d$ の非正規化)、他のライブラリと比較するときは必ず突き合わせてください。

**規約の食い違いは、物理的に意味のある差ではないのに数値が合わない、という最もありがちな混乱の原因です。**
:::

### 行列指数の計算法

$\exp(\mathcal{L}\Delta t)$ は級数をそのまま足しても収束が悪く、桁落ちします。標準的な方法は **scaling and squaring 法**です。

1. $\|\mathcal{L}\Delta t\|$ が小さくなるまで $2^s$ で割る(scaling)
2. その小さな行列に対して Padé 近似(13次)を適用
3. 結果を $s$ 回二乗する(squaring)。$\left(e^{M/2^s}\right)^{2^s} = e^{M}$

Padé近似は有理関数近似で、同じ次数のTaylor展開より精度が高くなります。Yuragi-Striderは `scaling_squaring_pade13` を使います。

## 8.7 なぜ構成上CPTPが保証されるのか

$\mathcal{L}$ がGKSL形の生成子であれば、$e^{\mathcal{L}t}$ は任意の $t \ge 0$ に対してCPTP写像です。これはGKS/Lindblad定理の逆向きの主張です。

したがって明示的CPTP経路では、**打ち切り誤差によるCP性の破れが原理的に起きません**。第7章のRK4が指数関数を4次多項式で近似していたのに対し、こちらは指数関数を(数値的に高精度で)そのまま計算しているからです。

| | RK4 | 明示的CPTP |
|---|---|---|
| 各区間の計算 | 数値積分(4次) | 行列指数(厳密) |
| 有限ステップでのCPTP性 | 保証されない | **構成上保証** |
| 密度行列の整形 | 各ステップで適用 | **適用しない** |
| 計算コスト | 低い | 高い |

:::tip[整形を「しない」ことが品質の証明になっている]
明示的CPTP経路が整形を適用しないのは、手を抜いているからではありません。**整形が必要になるということは、その経路がCPTP性を保てていないということ**だからです。整形しないで済むこと自体が、この経路の正しさの表明になっています。
:::

## 8.8 それでも「厳密なCPTP積分」ではない

重要な但し書きがあります。

:::warning[保証されるのは区間ごとのCPTP性です]
$e^{\mathcal{L}\Delta t}$ がCPTPであるのは、**その区間内で生成子 $\mathcal{L}$ が定数である**という前提の下でのみです。

時間依存のパルスに対しては、区間の中点の生成子を使う区分定数近似(`midpoint_piecewise_constant_v1`)が適用されます。したがって、連続的な時間依存問題に対する厳密な積分ではありません。

**各区間が個別にCPTPであることと、全体が正しい解に収束することは別の問題です。** 区間を細かくすれば近似は改善しますが、「物理的に正しい状態に見えるが、正しい状態ではない」という状況はありえます。

CPTP経路は「非物理的な状態を出さない」ことを保証しますが、「正しい答えを出す」ことは保証しません。後者は収束性の問題であり、[検証](../physics-model/validations/propagation.md)で別途確認する必要があります。
:::

これは誠実さの問題です。CPTPというラベルが付いているからといって、無条件に信頼してよいわけではありません。Yuragi-Striderが凍結された契約(`explicit_cptp_midpoint_gksl_v1`)の判定を **PASS WITH RESTRICTIONS** としているのは、この区別を明示するためです。

## 8.9 計算コストの壁

Liouvillian は $d^2 \times d^2$、Choi行列も $d^2\times d^2$ の行列です。$n$ 量子ビットでは $d = 2^n$ なので

$$
d^2 \times d^2 = 4^n \times 4^n
$$

| $n$ | 密度行列 $d\times d$ | Liouvillian $d^2\times d^2$ |
|---|---|---|
| 2 | $4\times4$ | $16\times16$ |
| 3 | $8\times8$ | $64\times64$ |
| 5 | $32\times32$ | $1024\times1024$ |
| 8 | $256\times256$ | $65536\times65536$ |

行列指数と固有値計算のコストは行列サイズの3乗で効くので、5量子ビットでは $1024^3 \approx 10^9$、8量子ビットでは $65536^3 \approx 3\times10^{14}$ の演算になります。

**このため、明示的CPTPが使えるのは密度行列経路より狭い範囲だけです。**

| 条件 | 挙動 |
|---|---|
| ノイズあり・6量子ビット以上でCPTPを要求 | **拒否**(`UNSUPPORTED_EVOLUTION_METHOD`) |
| 5量子ビット以上の条件付き回路でCPTPを要求 | **RK4へ強制フォールバック** |

密度行列経路そのものは8量子ビットまで動きます。CPTPだけが5量子ビットで頭打ちになるのは、扱う対象が密度行列($4^n$ 要素)ではなく**超演算子とChoi行列**($16^n$ 要素)だからです。条件付き回路が別扱いなのは、古典分岐ごとに別々の写像を作って監査する必要があるためです。

フォールバックは診断 `evolution_method_fallback` と警告としてクライアントに通知されます。

第1章で見た $2^n$ / $4^n$ の壁が、ここでもう一段厳しく現れているわけです。

## 実装ではどうなっているか

- Choi監査の規約、凍結された契約、フォールバック条件: [明示的CPTP写像](../physics-model/propagation/CPTP.md)
- 返される診断項目: [出力](../physics-model/outputs.md)
- KrausとChoiの原典: [参考文献](../physics-model/references.md)

## 演習

1. 振幅減衰の $K_0, K_1$ について $K_0^\dagger K_0 + K_1^\dagger K_1 = I$ を確かめよ。$\mathcal{E}(\rho)$ の $\rho_{11}$ 成分が $(1-p)\rho_{11}$ になることも示せ。

2. 8.3節の部分転置の計算を自分で実行し、固有値 $\frac{1}{2},\frac{1}{2},\frac{1}{2},-\frac{1}{2}$ を確かめよ。

3. 恒等写像 $\mathcal{E} = \mathcal{I}$ のChoi行列を計算し、それが最大エンタングル状態($\times d$)になることを示せ。固有値はどうなるか。

4. $\operatorname{vec}(AXB) = (B^{\mathsf{T}}\otimes A)\operatorname{vec}(X)$ を $2\times2$ の具体例で確かめよ(列優先)。

5. 完全に脱分極するチャネル $\mathcal{E}(\rho) = I/2$ はCPTPか。Kraus表現を与えて確かめよ。

6. 🔬 IDEの下の**メッセージ**「実行」タブにある「計算方法」→「時間発展の解法」を「固定ステップ RK4」と「明示的 CPTP 写像」に切り替え、同じ回路・環境で2回実行せよ。各結果を**状態エクスプローラー**で開き、同じ最終時刻の「Gate-aware 密度行列」と「最終純度」を記録して差を比較する。明示的CPTPで監査に失敗した場合はメッセージの「警告と問題」タブに表示されるため、警告がないことも確認せよ。

7. 🔬 IDEの左の**アルゴリズム**で5量子ビットの「3量子ビット反復符号」を読み込む。メッセージ「実行」タブの「計算方法」→「時間発展の解法」で「明示的 CPTP 写像」を選んで実行し、メッセージの「警告と問題」タブを開け。測定と条件付き補正を含むこの5量子ビット回路では主軌道がRK4へ切り替わるため、その警告が表示されることを確認せよ。

---

最後の章では、得られた密度行列を1つの数にまとめる評価指標を扱う予定です。
