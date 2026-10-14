---
title: 3. 開放量子系とGKSL方程式
sidebar_position: 4
---

# 3. 開放量子系とGKSL方程式

:::info[このページで学ぶこと]
- 環境と結合した系がなぜ非ユニタリに見えるのか
- 位相ダンピングを最小限のモデルから導出する
- GKSL(Lindblad)方程式の形と、なぜ**この形しかありえない**のか
- ジャンプ項と反交換子項がそれぞれ担う役割
- Born-Markov近似の内容と、それが破れる場面

**対応する処理レイヤー**: 入力(環境 → 方程式の構成)
:::

## 3.1 閉じた系と開いた系

第2章のvon Neumann方程式 $\dot\rho = -i[H,\rho]$ は、**外界から完全に孤立した系**の記述です。純度は保存され、状態は永遠に回り続けます。

しかし実際の量子ビットは孤立していません。基板のフォノン、制御線を伝わってくる電磁場のゆらぎ、近傍の二準位欠陥、磁束のノイズ。これらをまとめて**環境**(熱浴、bath)と呼びます。

ここで重要なのは次の点です。

> **系 + 環境を合わせた全体は、依然としてユニタリに発展します。**

非ユニタリ性は、全体のうち系だけを見る(部分トレースをとる)ことから生じます。第1章で見たとおり、全体が純粋でも部分系は混合になりうるからです。

$$
\rho_S(t) = \operatorname{Tr}_E\!\left[\,U_{SE}(t)\,\big(\rho_S(0)\otimes\rho_E\big)\,U_{SE}^\dagger(t)\,\right]
$$

**デコヒーレンスとは、系の情報が環境へ漏れ出していく過程を、系の側から見た姿**です。情報は消滅していません。ただ、こちらからは取り返せない場所に行っただけです。

## 3.2 最小の例 — 位相ダンピングを導出する

抽象論に入る前に、具体例を1つ完全に解いておきます。

系は1量子ビット、環境も1量子ビットとし、両者が次のユニタリで相互作用するとします(制御回転)。

$$
U_{SE} = |0\rangle\langle0|_S \otimes I_E + |1\rangle\langle1|_S \otimes R_E
$$

つまり「系が $|1\rangle$ のときだけ環境の状態が $R_E$ で回される」というモデルです。系のエネルギーは変化しないことに注意してください。

系の初期状態を一般に $\rho_S = \begin{pmatrix}\rho_{00} & \rho_{01}\\ \rho_{10} & \rho_{11}\end{pmatrix}$、環境を $|e_0\rangle$ とします。全体に $U_{SE}$ を作用させて環境を部分トレースすると

$$
\rho_S' =
\begin{pmatrix}
\rho_{00} & \rho_{01}\,\langle e_0|R_E|e_0\rangle^{*} \\[4pt]
\rho_{10}\,\langle e_0|R_E|e_0\rangle & \rho_{11}
\end{pmatrix}
$$

$\kappa = \langle e_0|R_E|e_0\rangle$ とおくと $|\kappa| \le 1$ です。ここから読み取れることは3つあります。

1. **対角成分(population)は変化しない** — エネルギーのやりとりがないから
2. **非対角成分(coherence)は $|\kappa|$ 倍に縮む** — これがデコヒーレンス
3. $|\kappa| = 1$ になるのは $R_E$ が $|e_0\rangle$ を動かさないときだけ

つまり、**環境が系の状態を「知って」しまうと($\kappa$ が小さくなると)、コヒーレンスが失われます**。環境が系の情報をどれだけ獲得したかと、系がどれだけコヒーレンスを失うかは、同じ現象の裏表です。

これを微小時間ごとに繰り返せば、$\rho_{01}(t) \propto |\kappa|^{t/\delta t}$ となり、**指数減衰**が現れます。以下で導く $\rho_{01}(t) = \rho_{01}(0)e^{-\gamma_\phi t}$ は、この繰り返しの連続極限です。

## 3.3 GKSL方程式

一般の環境に対して部分トレースを厳密に実行するのは不可能です。そこで近似を導入します。

### 3つの近似

**(1) Born近似(弱結合)**
系と環境の結合が弱く、環境は系から受ける影響を無視できるほど大きい。全体の状態が常に $\rho_{SE}(t) \approx \rho_S(t)\otimes\rho_E$ と積の形に保たれると仮定します。

**(2) Markov近似(記憶がない)**
環境の相関時間 $\tau_E$ が、系のダイナミクスの時間スケール $\tau_S$ よりずっと短い($\tau_E \ll \tau_S$)。環境は系から受け取った情報を即座に散逸させ、系に返してこない。したがって $\dot\rho_S(t)$ は**現在の** $\rho_S(t)$ だけで決まり、過去の履歴に依存しません。

**(3) 永年近似(回転波近似)**
系の固有周波数に比べて速く振動する項を平均して落とす。

これらの下で、系の時間発展は次の形に必ず帰着します。

$$
\boxed{\
\frac{d\rho}{dt}
= \underbrace{-i\big[H(t), \rho\big]}_{\text{コヒーレント}}
+ \underbrace{\sum_k \left(
L_k \rho L_k^\dagger
- \frac{1}{2}\left\{L_k^\dagger L_k, \rho\right\}
\right)}_{\text{散逸}}
\ }
$$

これが**GKSL方程式**(Gorini–Kossakowski–Sudarshan–Lindblad方程式、通称Lindblad方程式)です。$\{A,B\} = AB+BA$ は反交換子、$L_k$ は**崩壊演算子**(collapse operator, jump operator)と呼ばれます。

### なぜこの形しかないのか

GKSL方程式が重要なのは、単に「よく使われる近似」だからではありません。

> **定理(GKS 1976, Lindblad 1976)**: 完全正値かつトレース保存な時間一様半群の生成子は、必ず上の形に書ける。

第8章で詳しく扱いますが、物理的に許される状態変化は**CPTP写像**でなければなりません。「時間について一様(マルコフ的)なCPTP写像の族」を作ろうとすると、その生成子の形が数学的に一意に定まってしまう、というのがこの定理の内容です。

$$
\text{マルコフ性} + \text{完全正値性} + \text{トレース保存} \ \Longrightarrow\ \text{GKSL形}
$$

**GKSL方程式は仮定ではなく帰結です。** マルコフ的な開放量子系を書きたければ、この形を使うしかありません。

### 各項の意味

散逸項を2つに分けて読みます。

| 項 | 呼び名 | 役割 |
|---|---|---|
| $L_k \rho L_k^\dagger$ | ジャンプ項 | 「事象が起きた」場合の状態の移動 |
| $-\frac{1}{2}\{L_k^\dagger L_k, \rho\}$ | no-jump項 | 「事象が起きなかった」ことによる重みの減少 |

$L = \sqrt{\gamma}\,\sigma^-$(エネルギー緩和)を例にとります。$\sigma^- = |0\rangle\langle1|$ なので

- ジャンプ項 $\gamma\,\sigma^-\rho\,\sigma^+$ は $|1\rangle\langle1|$ の重みを $|0\rangle\langle0|$ へ移します(光子を1つ環境へ放出)
- no-jump項 $-\frac{\gamma}{2}\{|1\rangle\langle1|, \rho\}$ は、$|1\rangle$ が関わるすべての成分を減衰させます

2つが合わさって、確率の総和が保たれます。

### トレース保存の確認

散逸項のトレースをとると、トレースの巡回性 $\operatorname{Tr}(ABC) = \operatorname{Tr}(CAB)$ から

$$
\operatorname{Tr}\!\left[L\rho L^\dagger - \tfrac{1}{2}\{L^\dagger L,\rho\}\right]
= \operatorname{Tr}\!\left[L^\dagger L\rho\right] - \tfrac{1}{2}\operatorname{Tr}\!\left[L^\dagger L\rho\right] - \tfrac{1}{2}\operatorname{Tr}\!\left[\rho L^\dagger L\right]
= 0
$$

交換子項も $\operatorname{Tr}[H\rho - \rho H] = 0$ です。したがって $\frac{d}{dt}\operatorname{Tr}\rho = 0$、**トレースは厳密に保存されます**。係数 $\frac{1}{2}$ はこのために必要な値であり、任意には選べません。

:::note[レートは非負でなければならない]
$L_k = \sqrt{\gamma_k}\,A_k$ と書いたとき、$\gamma_k \ge 0$ が必要です。負のレートは完全正値性を壊し、密度行列の固有値が負になりうる非物理的な方程式になります。

Yuragi-Striderで `ideal_reference: true` を指定するとすべてのレートが 0 になりますが、負にすることはできません。
:::

## 3.4 解いてみる — 振幅減衰

$H = 0$、$L = \sqrt{\gamma}\,\sigma^-$ の場合を成分ごとに書き下します。$\sigma^+\sigma^- = |1\rangle\langle1|$ を使うと

$$
\frac{d\rho_{11}}{dt} = -\gamma\rho_{11},
\qquad
\frac{d\rho_{00}}{dt} = +\gamma\rho_{11},
\qquad
\frac{d\rho_{01}}{dt} = -\frac{\gamma}{2}\rho_{01}
$$

解は

$$
\rho_{11}(t) = \rho_{11}(0)\,e^{-\gamma t},
\qquad
\rho_{01}(t) = \rho_{01}(0)\,e^{-\gamma t/2}
$$

**重要な結果**: population は $\gamma$ で減衰しますが、coherence は $\gamma/2$ で減衰します。エネルギー緩和はそれ自体が位相もこわしますが、その速さは半分です。この係数 $1/2$ が、次章の $1/T_2 = \frac{1}{2T_1} + \frac{1}{T_\phi}$ の第1項の由来です。

## 3.5 純位相緩和

エネルギーを交換せず位相だけを壊す過程は、$L_\phi \propto \sigma_z$ で表されます。$\sigma_z$ は対角なので population を動かしません。

Yuragi-Striderの規約は次のとおりです。

$$
L_\phi = \sqrt{\frac{\gamma_\phi}{2}}\,\sigma_z
$$

なぜ $\sqrt{\gamma_\phi}$ ではなく $\sqrt{\gamma_\phi/2}$ なのかを計算で確かめます。$L_\phi^\dagger L_\phi = \frac{\gamma_\phi}{2}I$ なので、反交換子項は $-\frac{\gamma_\phi}{2}\rho$ です。ジャンプ項は $\frac{\gamma_\phi}{2}\sigma_z\rho\sigma_z$ で、その $(0,1)$ 成分は $\sigma_z$ の固有値 $(+1)$ と $(-1)$ が掛かるので $-\frac{\gamma_\phi}{2}\rho_{01}$ になります。合わせると

$$
\frac{d\rho_{01}}{dt} = -\frac{\gamma_\phi}{2}\rho_{01} - \frac{\gamma_\phi}{2}\rho_{01} = -\gamma_\phi\,\rho_{01}
$$

$$
\Longrightarrow \quad \rho_{01}(t) = \rho_{01}(0)\,e^{-\gamma_\phi t}
$$

:::warning[係数の規約は物理ではなく定義です]
もし $L_\phi = \sqrt{\gamma_\phi}\,\sigma_z$ と定義すれば、減衰は $e^{-2\gamma_\phi t}$ になります。どちらが「正しい」かは、**$\gamma_\phi$ を何の減衰率と呼ぶかを決めて初めて決まります**。

Yuragi-Striderは「$\gamma_\phi$ = 非対角成分の減衰率」という定義を採用し、そのために係数 $\sqrt{\gamma_\phi/2}$ を選んでいます。文献を参照するときは、必ずこの規約を確認してください。この規約は検証V4で解析解に対して確かめられています([散逸モデル](../physics-model/dissipation-model.md))。
:::

## 3.6 Blochベクトルで見る

1量子ビットのGKSL方程式は、Blochベクトルの運動方程式として書き直せます。$\gamma_{\text{pop}} = \gamma_\downarrow + \gamma_\uparrow$ とおくと、散逸だけの場合は

$$
\dot r_x = -\left(\frac{\gamma_{\text{pop}}}{2} + \gamma_\phi\right) r_x,
\quad
\dot r_y = -\left(\frac{\gamma_{\text{pop}}}{2} + \gamma_\phi\right) r_y,
\quad
\dot r_z = -\gamma_{\text{pop}}\left(r_z - r_z^{\text{eq}}\right)
$$

幾何学的な描像が明確になります。

- **横成分($x, y$)** は $1/T_2 = \frac{\gamma_{\text{pop}}}{2} + \gamma_\phi$ の速さで 0 に縮む
- **縦成分($z$)** は $1/T_1 = \gamma_{\text{pop}}$ の速さで平衡値 $r_z^{\text{eq}}$ へ向かう

Bloch球は時間とともに扁平な楕円体へと潰れていき、最後は平衡点の1点に収束します。ユニタリ発展が「回転」だったのに対し、散逸は「収縮」です。

この式から $T_2 \le 2T_1$ という関係が自動的に出ることにも注意してください。$\gamma_\phi \ge 0$ だからです。

## 3.7 近似が破れるとき

GKSL方程式は万能ではありません。次の場合、マルコフ近似は成り立ちません。

| 状況 | 何が起きるか |
|---|---|
| **$1/f$ ノイズ** | 低周波成分が強く、環境に長い記憶がある |
| **強結合** | Born近似が破れ、系と環境を分離できない |
| **構造化された環境** | 特定周波数に鋭いピークがあると、情報が系に戻ってくる |

実際の超伝導量子ビットの位相緩和は $1/f$ 磁束ノイズが主因で、厳密にはマルコフ的ではありません。この場合、コヒーレンスの減衰は指数関数 $e^{-\gamma t}$ ではなくガウス的な $e^{-(t/T_2)^2}$ に近づきます。

:::warning[Yuragi-Striderの立場]
Yuragi-Striderは磁束ノイズを、**マルコフ的な定数レート $\gamma_\phi$ に押し込める**という近似で扱っています。実際の $1/f$ スペクトルを再現するものではありません。

例外的に、Pulse-levelモデルには**準静的ノイズ**の枠があります。これは「ショットごとに固定され、1ショット内では変化しない離調のゆらぎ」で、$1/f$ ノイズの最も低周波な成分に対応する非マルコフ的な効果です。第6章で扱います。

この近似の限界は[前提と適用範囲](../physics-model/assumuptions.md)に明記されています。
:::

## 実装ではどうなっているか

- 方程式の全体像と時間区間への分解: [Lindblad方程式](../physics-model/lindblad.md)
- 3種類の崩壊演算子と係数規約: [散逸モデル](../physics-model/dissipation-model.md)
- 原典と「その文献だけでは支えられないもの」: [参考文献](../physics-model/references.md)

## 演習

1. $L = \sqrt{\gamma}\,\sigma^-$ の場合について、$\frac{d\rho_{01}}{dt} = -\frac{\gamma}{2}\rho_{01}$ を GKSL方程式から直接導け。

2. $L_\downarrow = \sqrt{\gamma_\downarrow}\sigma^-$ と $L_\uparrow = \sqrt{\gamma_\uparrow}\sigma^+$ が両方ある場合、$\dot\rho_{11} = -\gamma_\downarrow\rho_{11} + \gamma_\uparrow\rho_{00}$ となることを示せ。定常状態($\dot\rho_{11}=0$)での $\rho_{11}$ を求めよ。

3. 3.6節のBloch方程式から $T_2 \le 2T_1$ を示せ。等号が成り立つのはどんなときか。

4. 🔬 IDEの**回路エディター**でq0に H を1つ置き、右の**計算パラメーター**の「総シミュレーション時間 [μs]」を回路時間より十分長くして実行せよ。**状態エクスプローラー**の「環境レート」パネルを開き、「占有数緩和レート」$\Gamma_1$ と「純位相緩和レート」$\gamma_\phi$ から $T_2=1/(\Gamma_1/2+\gamma_\phi)$ を求める。**状態エクスプローラー**の「指標タイムライン」で純度を読み、$P(t)=(1+e^{-2t/T_2})/2$ と比較せよ。

5. 🔬 同じ結果を**状態エクスプローラー**で開き、「Bloch球」と「密度行列」で「理想状態(ノイズなし)」側とGate-aware側を見比べよ。理想側はBloch球の表面に留まる一方、Gate-aware側だけが内側へ縮むことを確かめる。理想側はアプリが同じ回路から自動生成するため、追加の設定は不要である。この差がGKSL方程式の散逸項を除いた場合に対応することを説明せよ。

---

次章では、環境パラメータから具体的なレート $\gamma_\downarrow, \gamma_\uparrow, \gamma_\phi$ がどう決まるかを扱います。→ [4. デコヒーレンスと有限温度](./decoherence.md)
