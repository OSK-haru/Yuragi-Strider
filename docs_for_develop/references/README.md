# Yuragi-Strider 参考文献台帳

## 目的

このフォルダは、Yuragi-Striderの物理モデル、数値実装、評価方法について、
どの文献を何の根拠として参照したかを追跡するための台帳である。

単なる文献一覧ではなく、各文献について次を区別する。

1. 文献が述べている内容
2. Yuragi-Striderで使用した考え方
3. 使用したコード・仕様・検証文書
4. 文献からは正当化されないYuragi-Strider固有の判断

## 引用区分

| 区分 | 意味 |
|---|---|
| `FOUNDATIONAL` | 数学的・物理的枠組みの原典 |
| `MODEL BASIS` | 採用モデルの構造を支える文献 |
| `METHOD BASIS` | 数値方式または評価方法を支える文献 |
| `VALIDATION BASIS` | 比較・監査方針を支える文献 |
| `FUTURE AUDIT` | 現在は未実施だが、今後の監査設計で使用する文献 |
| `PROJECT DECISION` | 文献の直接的帰結ではないプロジェクト固有判断 |

## 分類

- [開放量子系・散逸モデル](01_open_quantum_systems.md)
- [量子チャネル・CPTP・Choi監査](02_quantum_channels_cptp.md)
- [Pulse・transmon・qutrit・DRAG](03_pulse_transmon_control.md)
- [数値計算・独立solver監査](04_numerical_methods_and_solver_validation.md)
- [評価指標・実機監査・不確かさ](05_metrics_and_hardware_validation.md)
- [文献から直接は決まらない設計判断](06_project_specific_decisions.md)

## 現行モデルとの対応

| Yuragi-Strider領域 | 主な文献分類 | 現在の状態 |
|---|---|---|
| Gate-aware Hamiltonian-Lindblad | 開放量子系 | 実装・V1-V7検証済み |
| Two-level Pulse RWA | Pulse/control | 実装・QuTiP比較済み |
| Qutrit transmon / leakage / DRAG | Pulse/control | 実装・QuTiP比較済み |
| Explicit CPTP Pulse | CPTP / 数値計算 | 実装・Choi監査・QuTiP比較済み |
| Gate-aware explicit CPTP | CPTP / 数値計算 | 次期実装対象 |
| Hardware observable audit | 評価・実機監査 | dataset contract済み、formal holdout未実施 |

## 重要な主張境界

文献との整合は、Yuragi-Striderが特定実機を校正済みであることを意味しない。

現時点で文献と検証が支えるのは、主に次である。

- 採用したGKSL/Lindblad、Kraus、Choi、transmon、DRAGの理論的位置づけ
- 同じ数式を独立solverで解いた場合の数値整合性
- 明示した条件内でのCPTP性、収束性、Python/Rust parity

現時点で支えないものは次である。

- 特定メーカー・特定chipに対する予測精度
- 利用者が与えるT1・Tφの値そのもの（入力であり、モデルの予測ではない）
- Born-Markov、RWA、三準位打ち切りが全条件で妥当であること
- formal hardware holdout監査の合格

## 書誌情報の方針

- DOIが存在する場合はDOI URLを正本リンクとする。
- 原著論文または査読付きレビューを優先する。
- ソフトウェア比較では、ソフトウェア論文と実際に固定したversionを分けて記録する。
- 論文PDFをリポジトリへ複製せず、書誌情報と公開リンクのみを保存する。
- 新しい物理機能を追加するときは、対応する分類文書へ「使用箇所」と
  「非対応範囲」を追記する。
