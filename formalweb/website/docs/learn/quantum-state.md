---
title: 1. 量子状態と密度行列
sidebar_position: 2
---

# 1. 量子状態と密度行列

:::info[このページで学ぶこと]
- 量子状態を複素ベクトルとして書く方法と、測定確率の求め方
- 状態ベクトルでは書けない状態があること、そこから密度行列が必要になる理由
- 密度行列の定義・性質・幾何学的な描像(Bloch球)
- 複数量子ビットの扱い、部分トレース、そして計算コストが $4^n$ になる理由

**対応する処理レイヤー**: 状態の表現(すべてのレイヤーの土台)
:::

## 1.1 状態ベクトル

1量子ビットの状態は、2つの複素数の組で表されます。

$$
|\psi\rangle = \alpha\,|0\rangle + \beta\,|1\rangle
= \begin{pmatrix} \alpha \\ \beta \end{pmatrix},
\qquad \alpha, \beta \in \mathbb{C}
$$

$|0\rangle$ と $|1\rangle$ は**計算基底**と呼ばれ、古典ビットの 0 と 1 に対応します。$\alpha$ と $\beta$ は**確率振幅**です。

この状態を計算基底で測定すると、結果は確率的に決まります。

$$
P(0) = |\alpha|^2, \qquad P(1) = |\beta|^2
$$

これを**Born則**といいます。確率の和が 1 でなければならないので、状態には規格化条件が課されます。

$$
|\alpha|^2 + |\beta|^2 = 1
$$

### 大域位相は観測できない

$|\psi\rangle$ と $e^{i\varphi}|\psi\rangle$ は、どんな測定をしても区別できません。確率は $|e^{i\varphi}\alpha|^2 = |\alpha|^2$ で変わらないからです。この $e^{i\varphi}$ を**大域位相**といい、物理的な意味を持ちません。

一方、$\alpha$ と $\beta$ の**相対的な**位相は観測できます。

$$
|+\rangle = \frac{1}{\sqrt{2}}\big(|0\rangle + |1\rangle\big),
\qquad
|-\rangle = \frac{1}{\sqrt{2}}\big(|0\rangle - |1\rangle\big)
$$

この2つは計算基底で測ると両方とも 50:50 ですが、Hadamardゲートを掛けてから測ると $|+\rangle \to |0\rangle$、$|-\rangle \to |1\rangle$ となり、確実に区別できます。相対位相は物理的な情報です。

:::note[実装との対応]
Yuragi-Striderのゲート分解では、分解前後で大域位相が変わることがあります(CPの分解は $e^{-i\theta/4}$ だけ異なる、など)。観測量には影響しませんが、密度行列を直接比較するときは注意が必要です。[Gate-awareモデル](../physics-model/control_models/gate-awaremodel.md)を参照してください。
:::

## 1.2 なぜ状態ベクトルでは足りないのか

ここで、性質のまったく違う2つの「わからなさ」を考えます。

**(A) 重ね合わせ**: 状態は確実に $|+\rangle$ である。測定すると 0 か 1 が 50:50 で出る。

**(B) 古典的混合**: コインを投げて、表なら $|0\rangle$、裏なら $|1\rangle$ を用意した。どちらかは知らない。測定すると 0 か 1 が 50:50 で出る。

計算基底で測る限り、この2つは同じ結果を与えます。しかし物理的にはまったく別物です。前節のとおり (A) にHadamardを掛けて測れば必ず 0 が出ますが、(B) にHadamardを掛けると $|+\rangle$ か $|-\rangle$ になり、やはり 50:50 のままです。

問題は、**(B) を状態ベクトルでは書けない**ことです。「$|0\rangle$ か $|1\rangle$ のどちらか」は1本のベクトルではありません。

環境ノイズが作用すると、系はまさにこの (B) の状態になります。したがって開放量子系を扱うには、状態ベクトルより広い枠組みが必要です。それが密度行列です。

## 1.3 密度行列

状態 $|\psi_i\rangle$ を確率 $p_i$ で持つ統計的な集団(アンサンブル)を、次の行列で表します。

$$
\rho = \sum_i p_i\, |\psi_i\rangle\langle\psi_i|,
\qquad p_i \ge 0, \quad \sum_i p_i = 1
$$

ここで $|\psi\rangle\langle\psi|$ は列ベクトルと行ベクトルの積で、$2\times2$ の行列になります。

先の2つの例を書き下してみます。

$$
\rho_A = |+\rangle\langle+| = \frac{1}{2}\begin{pmatrix} 1 & 1 \\ 1 & 1 \end{pmatrix},
\qquad
\rho_B = \frac{1}{2}|0\rangle\langle0| + \frac{1}{2}|1\rangle\langle1| = \frac{1}{2}\begin{pmatrix} 1 & 0 \\ 0 & 1 \end{pmatrix}
$$

**対角成分は同じで、非対角成分が違います。** これが (A) と (B) を分ける唯一の情報です。

| 成分 | 名前 | 意味 |
|---|---|---|
| 対角 $\rho_{00}, \rho_{11}$ | population(占有数) | 各基底状態が観測される確率 |
| 非対角 $\rho_{01}, \rho_{10}$ | coherence(コヒーレンス) | 基底間の位相関係が保たれている度合い |

**デコヒーレンスとは、非対角成分が減衰する現象のことです。** 第3章以降で扱う散逸項は、まさにこの $\rho_{01}$ を潰す働きをします。

### 密度行列の性質

任意の密度行列は次の3条件を満たし、逆にこの3条件を満たす行列はすべて物理的に許される状態です。

$$
\rho = \rho^\dagger
\quad(\text{エルミート}),
\qquad
\rho \succeq 0
\quad(\text{半正定値}),
\qquad
\operatorname{Tr}\rho = 1
\quad(\text{トレース1})
$$

半正定値とは、すべての固有値が 0 以上ということです。固有値は「その固有状態を持つ確率」に対応するので、負であってはいけません。

:::warning[この3条件が数値計算の生命線です]
シミュレーターが返す密度行列がこの条件を破っていたら、それは物理的な状態ではなく数値誤差です。Yuragi-Striderが `raw_minimum_eigenvalue` や `max_trace_error` を診断として返しているのは、この3条件が保たれているかを利用者が確認できるようにするためです。詳しくは[第7章](./numerical-integration.md)で扱います。
:::

### 観測量の期待値

演算子 $A$ の期待値は、密度行列から次のように得られます。

$$
\langle A \rangle = \operatorname{Tr}(\rho A)
$$

状態ベクトルの場合の $\langle\psi|A|\psi\rangle$ を、アンサンブル平均まで含めて一般化した式です。実際、$\rho = |\psi\rangle\langle\psi|$ を代入すると $\operatorname{Tr}(|\psi\rangle\langle\psi|A) = \langle\psi|A|\psi\rangle$ に戻ります。

測定確率もこの形で書けます。基底状態 $|k\rangle$ が観測される確率は $A = |k\rangle\langle k|$ とおいて

$$
P(k) = \operatorname{Tr}\big(\rho\,|k\rangle\langle k|\big) = \rho_{kk}
$$

つまり**出力確率は密度行列の対角成分そのもの**です。

## 1.4 純度 — 混ざり具合を測る

状態がどれだけ「混ざっている」かを1つの数で表す量が**純度**です。

$$
P = \operatorname{Tr}(\rho^2)
$$

$d$ 次元系で $1/d \le P \le 1$ の範囲をとります。

- $P = 1$ ⟺ $\rho = |\psi\rangle\langle\psi|$ と書ける(**純粋状態**)
- $P = 1/d$ ⟺ $\rho = I/d$(**最大混合状態**、完全に情報が失われた状態)

先の例では $\operatorname{Tr}(\rho_A^2) = 1$、$\operatorname{Tr}(\rho_B^2) = 1/2$ です。$\rho_B$ は1量子ビットの最大混合状態でした。

純粋状態の条件は $\rho^2 = \rho$ とも書けます(射影演算子であること)。

## 1.5 Bloch球 — 1量子ビットの幾何学

1量子ビットの密度行列はエルミートでトレース1なので、実質的な自由度は3つです。Pauli行列

$$
\sigma_x = \begin{pmatrix} 0 & 1 \\ 1 & 0 \end{pmatrix},
\quad
\sigma_y = \begin{pmatrix} 0 & -i \\ i & 0 \end{pmatrix},
\quad
\sigma_z = \begin{pmatrix} 1 & 0 \\ 0 & -1 \end{pmatrix}
$$

を使うと、任意の1量子ビット状態は3次元の実ベクトル $\vec{r} = (r_x, r_y, r_z)$ で一意に書けます。

$$
\rho = \frac{1}{2}\big(I + r_x\sigma_x + r_y\sigma_y + r_z\sigma_z\big),
\qquad
r_j = \operatorname{Tr}(\rho\,\sigma_j)
$$

この $\vec{r}$ を**Blochベクトル**といいます。半正定値条件は $|\vec{r}| \le 1$ と同値で、状態全体は半径1の球(**Bloch球**)を埋めます。

$$
\operatorname{Tr}(\rho^2) = \frac{1 + |\vec{r}|^2}{2}
$$

| 位置 | 状態 |
|---|---|
| 球面 $\lVert\vec{r}\rVert = 1$ | 純粋状態 |
| 内部 $0 \lt \lVert\vec{r}\rVert \lt 1$ | 混合状態 |
| 中心 $\vec{r} = 0$ | 最大混合状態 |
| 北極 $\vec{r} = (0,0,1)$ | $\lvert0\rangle$ |
| 南極 $\vec{r} = (0,0,-1)$ | $\lvert1\rangle$ |
| 赤道上 $\vec{r} = (1,0,0)$ | $\lvert+\rangle$ |

この描像を使うと、これから学ぶ物理過程が視覚的に理解できます。

- **ユニタリゲート** = Bloch球の**回転**(長さを変えない)
- **エネルギー緩和 $T_1$** = 北極へ向かう収縮
- **位相緩和 $T_\phi$** = $z$ 軸へ向かう収縮(赤道方向の成分だけが縮む)

## 1.6 複数の量子ビット

$n$ 量子ビットの状態空間は、1量子ビットの空間のテンソル積です。次元は $2^n$、密度行列は $2^n \times 2^n$ になります。

$$
|01\rangle = |0\rangle \otimes |1\rangle
= \begin{pmatrix}1\\0\end{pmatrix} \otimes \begin{pmatrix}0\\1\end{pmatrix}
= \begin{pmatrix}0\\1\\0\\0\end{pmatrix}
$$

:::note[ビット順の規約]
Yuragi-Striderでは **q0 が最上位ビット** です。2量子ビットの基底ラベル `"01"` は q0 = 0、q1 = 1 を意味します。この規約は文献や他のライブラリで異なることがあるので、結果を比較するときは必ず確認してください。
:::

### エンタングルメントと部分トレース

すべての多体状態が $|\psi_A\rangle \otimes |\psi_B\rangle$ の形に書けるわけではありません。書けない状態を**エンタングルした状態**といいます。代表例がBell状態です。

$$
|\Phi^+\rangle = \frac{1}{\sqrt{2}}\big(|00\rangle + |11\rangle\big)
$$

このとき、片方の量子ビットだけに注目したらどうなるでしょうか。答えは**部分トレース**で与えられます。

$$
\rho_A = \operatorname{Tr}_B(\rho_{AB})
= \sum_k \big(I \otimes \langle k|\big)\,\rho_{AB}\,\big(I \otimes |k\rangle\big)
$$

Bell状態に対して計算すると

$$
\rho_A = \frac{1}{2}|0\rangle\langle0| + \frac{1}{2}|1\rangle\langle1| = \frac{I}{2}
$$

**全体は純粋状態なのに、部分系は最大混合状態です。** これは古典確率論には存在しない現象で、開放量子系の理論全体の出発点になります。

:::tip[これが第3章の核心です]
系と環境を合わせた全体はユニタリに発展します。しかし系と環境がエンタングルすると、系だけを見た $\rho_S = \operatorname{Tr}_E(\rho_{SE})$ は純度を失います。**デコヒーレンスとは、情報が環境へ漏れ出す現象を系の側から見たもの**です。
:::

## 1.7 計算コスト

$n$ 量子ビットの密度行列は $2^n \times 2^n = 4^n$ 個の複素数を持ちます。状態ベクトルの $2^n$ 個と比べて指数関数的に不利です。

| $n$ | 状態ベクトル | 密度行列 |
|---|---|---|
| 5 | 32 | 1,024 |
| 10 | 1,024 | 1,048,576 |
| 18 | 262,144 | $6.9\times10^{10}$ |

Yuragi-Striderが**ノイズありで8量子ビットまで、理想条件の状態ベクトルで18量子ビットまで**という非対称な上限を持つのは、この $2^n$ と $4^n$ の差が理由です。さらに第8章で扱うCPTP経路はLiouvillian超演算子($4^n \times 4^n$ の行列)を扱うため、もう一段厳しくなります。

## 実装ではどうなっているか

- 密度行列とその出力形式: [出力](../physics-model/outputs.md)
- 状態ベクトル経路が選ばれる条件と上限: [状態ベクトル発展](../physics-model/propagation/statevector.md)
- ビット順と規模の上限: [Gate-awareモデル](../physics-model/control_models/gate-awaremodel.md)

## 演習

1. $\rho_A$ と $\rho_B$ のBlochベクトルをそれぞれ求めよ。純度が $\operatorname{Tr}(\rho^2) = (1+|\vec r|^2)/2$ に一致することを確かめよ。

2. $\rho = \frac{1}{2}(I + r_z\sigma_z)$ の固有値を求め、半正定値条件が $|r_z| \le 1$ と同値であることを示せ。

3. $|\Phi^+\rangle\langle\Phi^+|$ を $4\times4$ 行列として書き下し、部分トレースを実行して $\rho_A = I/2$ を確かめよ。

4. 🔬 IDEの**回路エディター**で2量子ビットのベル回路(q0にH、続けてq0を制御・q1を標的とするCNOT)を組み、ツールバーの「シミュレーションを実行」で実行せよ。実行後に作業領域を**状態エクスプローラー**に切り替え、「表示項目」から「密度行列」と「出力確率」を表示する。「理想状態の密度行列」で $\rho_{00,00}$、$\rho_{11,11}$ と非対角の $\rho_{00,11}$、$\rho_{11,00}$ がそれぞれ約0.5になること、出力確率の $|00\rangle$ と $|11\rangle$ が対角成分と一致することを確かめよ。

5. 🔬 同じBell回路のまま、IDEの右の**計算パラメーター**→「デバイス品質」を既定値から下げて再実行せよ。**状態エクスプローラー**の「Gate-aware 密度行列」を右側の「理想状態の密度行列」と比べ、$\rho_{00,11}$ と $\rho_{11,00}$ の絶対値がどう変わるかを観察する。「指標タイムライン」の最終純度も記録し、1との差を求めよ。

---

次章では、この状態が時間とともにどう動くかを扱います。→ [2. ハミルトニアンとユニタリ発展](./unitary-dynamics.md)
