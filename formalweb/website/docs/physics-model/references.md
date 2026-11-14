---
title: 参考文献
sidebar_position: 7
---

# 参考文献

Yuragi-Striderの物理モデル、数値実装、評価方法の根拠となる文献です。

各文献について「文献が述べていること」と「Yuragi-Striderが実際に使用した部分」は区別されます。文献に載っているからといって、Yuragi-Striderがその全体を実装しているわけではありません。逆に、文献からは正当化されないプロジェクト固有の判断も存在します。

## 開放量子系・散逸モデル

V. Gorini, A. Kossakowski, and E. C. G. Sudarshan, "Completely positive dynamical semigroups of N-level systems," *Journal of Mathematical Physics* **17**, 821–825 (1976). [DOI: 10.1063/1.522979](https://doi.org/10.1063/1.522979)

G. Lindblad, "On the generators of quantum dynamical semigroups," *Communications in Mathematical Physics* **48**, 119–130 (1976). [DOI: 10.1007/BF01608499](https://doi.org/10.1007/BF01608499)

A. A. Clerk, M. H. Devoret, S. M. Girvin, F. Marquardt, and R. J. Schoelkopf, "Introduction to quantum noise, measurement, and amplification," *Reviews of Modern Physics* **82**, 1155–1208 (2010). [DOI: 10.1103/RevModPhys.82.1155](https://doi.org/10.1103/RevModPhys.82.1155)

G. Ithier et al., "Decoherence in a superconducting quantum bit circuit," *Physical Review B* **72**, 134519 (2005). [DOI: 10.1103/PhysRevB.72.134519](https://doi.org/10.1103/PhysRevB.72.134519)

これらはGKSL方程式の構成、有限温度の上下遷移、緩和と純位相緩和の位置づけの根拠です。ただし、**Born-Markov近似がYuragi-Striderの全入力範囲で成立すること**や、**使用するレートが特定実機のレートと一致すること**は、これらの文献からは支持されません。

## 量子チャネル・CPTP・Choi監査

K. Kraus, "General state changes in quantum theory," *Annals of Physics* **64**, 311–335 (1971). [DOI: 10.1016/0003-4916(71)90108-4](https://doi.org/10.1016/0003-4916%2871%2990108-4)

M.-D. Choi, "Completely positive linear maps on complex matrices," *Linear Algebra and its Applications* **10**, 285–290 (1975). [DOI: 10.1016/0024-3795(75)90075-0](https://doi.org/10.1016/0024-3795%2875%2990075-0)

明示的CPTP経路のChoi行列による完全正値性・トレース保存性の監査は、これらに基づいています。

## Pulse・transmon・qutrit・DRAG

J. Koch et al., "Charge-insensitive qubit design derived from the Cooper pair box," *Physical Review A* **76**, 042319 (2007). [DOI: 10.1103/PhysRevA.76.042319](https://doi.org/10.1103/PhysRevA.76.042319)

F. Motzoi, J. M. Gambetta, P. Rebentrost, and F. K. Wilhelm, "Simple pulses for elimination of leakage in weakly nonlinear qubits," *Physical Review Letters* **103**, 110501 (2009). [DOI: 10.1103/PhysRevLett.103.110501](https://doi.org/10.1103/PhysRevLett.103.110501)

J. M. Gambetta, F. Motzoi, S. T. Merkel, and F. K. Wilhelm, "Analytic control methods for high-fidelity unitary operations in a weakly nonlinear oscillator," *Physical Review A* **83**, 012308 (2011). [DOI: 10.1103/PhysRevA.83.012308](https://doi.org/10.1103/PhysRevA.83.012308)

トランズモンのDuffing振動子近似、および漏れ抑制のためのDRAG制御の根拠です。

## 数値計算・独立ソルバー監査

N. J. Higham, "The scaling and squaring method for the matrix exponential revisited," *SIAM Journal on Matrix Analysis and Applications* **26**, 1179–1193 (2005). [DOI: 10.1137/04061101X](https://doi.org/10.1137/04061101X)

A. H. Al-Mohy and N. J. Higham, "A new scaling and squaring algorithm for the matrix exponential," *SIAM Journal on Matrix Analysis and Applications* **31**, 970–989 (2009). [DOI: 10.1137/09074721X](https://doi.org/10.1137/09074721X)

S. Blanes, F. Casas, J. A. Oteo, and J. Ros, "The Magnus expansion and some of its applications," *Physics Reports* **470**, 151–238 (2009). [DOI: 10.1016/j.physrep.2008.11.001](https://doi.org/10.1016/j.physrep.2008.11.001)

J. R. Johansson, P. D. Nation, and F. Nori, "QuTiP: An open-source Python framework for the dynamics of open quantum systems," *Computer Physics Communications* **183**, 1760–1772 (2012). [DOI: 10.1016/j.cpc.2012.02.021](https://doi.org/10.1016/j.cpc.2012.02.021)

行列指数の scaling and squaring 法(`scaling_squaring_pade13`)と、独立検証に用いるQuTiPの根拠です。QuTiPは**本番計算には使用せず**、検証専用です。

## 評価指標・実機監査・不確かさ

R. Jozsa, "Fidelity for mixed quantum states," *Journal of Modern Optics* **41**, 2315–2323 (1994). [DOI: 10.1080/09500349414552171](https://doi.org/10.1080/09500349414552171)

C. A. Fuchs and J. van de Graaf, "Cryptographic distinguishability measures for quantum-mechanical states," *IEEE Transactions on Information Theory* **45**, 1216–1227 (1999). [DOI: 10.1109/18.761271](https://doi.org/10.1109/18.761271)

C. J. Wood and J. M. Gambetta, "Quantification and characterization of leakage errors," *Physical Review A* **97**, 032306 (2018). [DOI: 10.1103/PhysRevA.97.032306](https://doi.org/10.1103/PhysRevA.97.032306)

E. Magesan, J. M. Gambetta, and J. Emerson, "Scalable and robust randomized benchmarking of quantum processes," *Physical Review Letters* **106**, 180504 (2011). [DOI: 10.1103/PhysRevLett.106.180504](https://doi.org/10.1103/PhysRevLett.106.180504)

R. Blume-Kohout et al., "Demonstration of qubit operations below a rigorous fault tolerance threshold with gate set tomography," *Nature Communications* **8**, 14485 (2017). [DOI: 10.1038/ncomms14485](https://doi.org/10.1038/ncomms14485)

M. C. Kennedy and A. O'Hagan, "Bayesian calibration of computer models," *Journal of the Royal Statistical Society: Series B* **63**, 425–464 (2001). [DOI: 10.1111/1467-9868.00294](https://doi.org/10.1111/1467-9868.00294)

Uhlmann忠実度、トレース距離、漏れ誤差の定量化の根拠です。ランダム化ベンチマーキング、ゲートセットトモグラフィ、ベイズ較正は**今後の監査設計のための参照**であり、現在は実施していません。

## 文献からは決まらない設計判断

次の項目は文献の直接的な帰結ではなく、Yuragi-Strider固有の判断です。

- ゲートの既定所要時間(H = 0.02 μs、CNOT = 0.20 μs など)
- 有効時間のしきい値 0.9
- qutritの $\gamma_{21}(T{=}0) = 2\gamma_{10}(T{=}0)$ という近似

これらは選んだ値であり、物理的な導出を持ちません。デバイス品質から $T_1$・$T_\phi$ への幾何補間と、磁束ノイズから位相緩和率への線形写像も以前はここにありましたが、根拠がないため2026-11-14に削除しました。

## 原典の台帳

引用区分(`FOUNDATIONAL` / `MODEL BASIS` / `METHOD BASIS` / `VALIDATION BASIS` / `FUTURE AUDIT` / `PROJECT DECISION`)を含む詳細な台帳は、リポジトリの `docs_for_develop/references/` に保守されています。各文献について、使用したコード箇所と「この文献だけでは支えないもの」が明記されています。
