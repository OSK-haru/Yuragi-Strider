# 攻撃シミュレーションモード設計

最終同期: 2026-10-08（D72の製品時計・画面接続を同期。詳細なコード確認日は進捗文書を参照）

本フォルダは、会話で検討してきた攻撃シミュレーションの開発用設計記録である。

## 位置づけ

この攻撃シミュレーターは、**仮想環境の中のセキュリティインシデントを模倣する**ための教育・研究用のシミュレーターである。

- 対象は、このアプリの中に作った**仮想の量子コンピューターと仮想のサービス**だけである。実在のクラウド・実機・他者のシステムには接続しない。資格情報もシミュレーター内だけで通用する仮のものである。
- 模倣するのは、共有量子計算機で論文が報告した種類の現象（不完全なResetによるテナント間の情報残留など）である。目的は、その現象がどの条件で起き、どの防御（Reset回数、配置の分離など）で抑えられるかを、物理モデルの上で確かめ、学ぶことにある。
- 現実のサービスに無い操作（交互観測など）は抽象コマンドとして分け、既定では無効にしている。前提と仮定は、環境ごとの仮定記録に残す。

個人教材の `learning/private/` や公開サイトの説明とは区別する。
**物理モデル基準版は固定済み。共通コマンドのdesktop／development経路をmountし、画面は製品`host_clock`（1x）・server pushに接続済み。explicit_waitは試験経路のみ。HTTP polling候補、in-process／WebSocket接続、Reset漏洩の内部orchestrator・推定器・hidden graderは存在するが、Reset課題の製品preset／採点接続、永続化・複数process復旧、管理者bootstrapは未完了。利用者のコードはブラウザ内のWorker（Pyodide）でだけ実行し、サーバー側では実行しない（[スクリプトアクセス](script-access.md)）。service timing値の根拠見直しは提案中で、未承認値は変更していない。**

## 読む順序

| 文書 | 内容 |
| --- | --- |
| [レイヤー別の議論整理](layered-discussion.md) | 目的から実装・品質までの全体像、合意・提案・未決定、直近の設計リスク |
| [サービス差とアクセス条件](service-and-access-model.md) | 三つの攻撃目的、サービス別の操作・観測、論文の仮定と実サービスの区別 |
| [一般サービスの特殊化条件](service-specialization-contract.md) | 要件との対応、比較範囲・対応写像・確認義務、仕様片の構成例、初回までの検討範囲 |
| [共通基盤v1の実装前契約](common-foundation-v1-contract-proposal.md) | D57の五暫定契約と受入条件、D58の複数qubit・大規模トポロジー、live時間のD60更新 |
| [通信・時間・被害者進行のサービス比較](service-time-and-workload-comparison.md) | IBM/AWSとの対応・不一致、D60のhost-clock／explicit-wait選択と固定周期を強制しないworkload、D54待機物理と検証計画 |
| [共通操作・観測・校正契約](common-command-observation-contract.md) | 主体非依存の操作catalog、疑似ターミナル、権限合成、統計量の来歴、内部校正との分離 |
| [回路JSON・Program要求境界](program-json-boundary.md) | Circuit Studio形式を共通送信要求へ包む境界と、限定回路compilerの対応範囲 |
| [共通コマンドclient](common-command-client.md) | 認証付きdevice照会・回路送信・job操作、実サービスのライフサイクルとの対応、Sandbox前の実装範囲 |
| [並列開発の監査修正](parallel/audit-repairs-2026-10-04.md) | サービス／UI接続境界と状態所有の修正、検証結果、残件 |
| [D53時刻境界の並列引継ぎ](parallel/time-frontier-handoff.md) | 時刻metadata部品、独立遅延検証、所有権／結果投影の修正範囲 |
| [共通基盤v1の並列引継ぎ](parallel/common-foundation-v1-handoff.md) | 全体metadata／局所状態、準備／継承、内部時計／有限workloadの実装と未接続境界 |
| [宣言workloadの受付接続](parallel/declared-workload-handoff.md) | 凍結JSON・clock owner・両主体の認証済み共通受付、予約／barrier／再送と物理commitの未接続境界 |
| [有限宣言列の物理確定](parallel/declared-physical-commit-handoff.md) | detached計算、待機／開始・物理batch／終了の原子的確定、共通結果公開とlive sealの残件 |
| [有限sourceの共通時間・実行接続](parallel/closed-workload-world-handoff.md) | clock／window／安全seal／結果公開の自動生成、両mode共通進行、応答喪失修正とlive HTTPの残件 |
| [live受付・投影・実所有の引継ぎ](parallel/live-ingress-boundaries-handoff.md) | D59/D60・共通live実行器、host_clock限定pump、暫定HTTP request_id/pollingとWebSocket置換境界 |
| [仮定・設定・根拠の取扱契約](assumption-handling-contract.md) | 主体・関係・実験への仮定分類、profileとoverride、根拠、可視性、整合性検査 |
| [決定事項](decisions.md) | ユーザーとの合意、具体化案、以前の案からの変更 |
| [構造と境界](architecture.md) | ワークスペース、サービス、被害者、演習管理の関係 |
| [実行境界・時間・矛盾解消方針](runtime-boundaries-and-resolutions.md) | 共通世界、局所実行領域、三種類の時間、処理落ち、観測経路、実装時の不変条件 |
| [局所物理近似の共通契約案](local-physics-approximation-contract-proposal.md) | 機構別の影響関係、局所計算領域、状態保持、近似の比較条件。未合意の提案 |
| [契約・検討領域の進捗台帳](contract-and-domain-progress.md) | 8契約と9検討領域の状態、次の決定、実装・検証証跡の索引 |
| [共有QPUサービス実行契約の提案](service-runtime-contract-proposal-2026-09-23.md) | [ジョブの語義](service-runtime-contract-proposal-2026-09-23.md#30-ジョブの語義合意済み具体的なschemaは未決定)、状態所有、queue、取消、失敗、排他 |
| [不完全Reset論文再現シナリオ契約](reset-leakage-paper-reproduction-contract.md) | CCS 2022の同一量子ビット・順次再利用によるReset越し漏洩を最初の縦切りへ落とした暫定契約 |
| [不完全Reset論文再現契約の欠陥監査](reset-leakage-paper-reproduction-audit.md) | 論文・契約・既存物理model・現行APIを突き合わせた実装前監査 |
| [Reset漏洩の利用者学習校正](reset-leakage-visible-training-calibration-2026-09-29.md) | 公開training結果だけからの推定器学習、必要な観測予算、確率的graderの残る判断 |
| [進捗と根拠](progress.md) | コードで確認した実装範囲、検証履歴、未実装範囲 |
| [未決定事項](open-questions.md) | 次に決める仕様と決定順序 |
| [2026-09-19の変更内容](changes-2026-09-19.md) | 今回の変更前後、維持した方針、未決定の範囲 |

## 目標

上位の目的は量子コンピューターを通したセキュリティインシデントの体験である。
製品全体の成立条件は「攻撃者から被害者へ届く、設定で宣言された共有資源・経路がある」こと。
共有QPUはその一つの対象環境であり、全課題の必須前提ではない。経路ごとの機構・権限・成立条件は別途定義する（[決定D48](decisions.md)、[レイヤー別整理 L1](layered-discussion.md#l1-目的対象範囲)）。

NISQは製品全体の不変条件ではない。実行方式はdevice／service profileが宣言し、現行の不完全Reset課題だけを`nisq_physical`対応とする。将来のFTQC profileを同じ製品構造へ追加できるが、NISQ用物理攻撃をそのままFTQC攻撃として扱わない。

主要な攻撃目的は、計算結果への干渉・改ざん、アルゴリズム／回路／命令内容の推定、処理に埋め込まれた秘密の推定である。
共有超電導QPUは中心的な対象候補であり、
不完全Resetによる残留、駆動クロストークやZZによる干渉、相関Readoutによる推定を、
時間的・空間的・観測上のテナント間隔離として扱う。
情報取得と計算への干渉を区別し、攻撃条件・影響・観測可能性・検知・緩和を評価する。
提供する体験は量子コンピューターを通したセキュリティインシデントであり、量子暗号は今回の対象外とする。

利用者が高機能な攻撃者環境でコードを書き、調査・許可された操作・分析・提出を行う。
ジョブ送信は主な操作候補だが、唯一の経路とは確定しない。
U-22で初めて触れる利用者にも、一直線に完走できるチュートリアルを用意する。
CTFの達成体験を使いつつ、プロジェクトの中心価値である因果関係の理解を保つ。
古典的ハッキングの実行を必須にせず量子固有の問題へ集中する。省略する準備・権限は課題の前提として明示する。

## 最新の製品構造

**一つの共通ワークスペースと実行基盤を本体とする。**
問題を開くと、そのワークスペースへ課題環境・攻撃者権限・被害者・予算・提出判定を設定する。
自由実験と課題形式はセッション方針として分け、別製品・別実行基盤にはしない。
ターミナル、コード編集、回路作成、結果分析は問題間で共通の道具とする。攻撃者と被害者は選択されたservice profile内で同じ主体非依存の操作catalogを使い、実効範囲だけを主体権限とScenarioPolicyで変える。

操作階層は次の二つに分ける。

1. **上位のシミュレーター設定**: 主体別の`AttackerConfig`・`VictimConfig`・`QpuServiceConfig`、主体間の`RelationConfig`、実験条件の`ExperimentConfig`を構成し、`ScenarioPolicy`で編集・秘匿・提出を統制する。内部v0 schemaとcompilerは実装済み、永続化用の正式schemaは未決定。CTF型ではこれらを束ねたプリセットを読み込む。
2. **課題内の攻撃側画面**: 利用者が操作・攻撃できる唯一の環境。被害者と実機は設定に従って背後で動作する。

同じ共通基盤の設定階層と実行階層を用い、`free`では環境作成者による設定編集を許し、`graded`ではプリセットと提出条件を固定する。
仮定と環境管理者の設定は両modeとも成立前だけ変更可能とし、変更後は新規環境として開始する。環境管理者はメタ層、参加者の仮想資格情報発行はサービス層へ置く（D52）。
処理能力超過時のフォールバックは要求の競合・重複実行を防ぐ復旧再開とし、設定を解凍する新規開始とは区別する（D51）。具体的な復旧・管理者認証は未実装。詳細は[実行境界](runtime-boundaries-and-resolutions.md#42-フォールバックとしての再開2026-10-04合意)と[資格情報境界](credential-delivery-boundary.md#環境管理者とサービス発行の境界2026-10-04合意)を参照。
課題中に上位設定へ戻る際の表示・棄却と、新規開始後の提出資格の扱いは未決定。

## 共通基盤の現在の考え方

共通化するのは「全サービスで同じ攻撃ができること」ではなく、
サービスごとの権限・実行方式・観測範囲を表し、その範囲で調査・操作・評価する仕組みである。
装置の物理特性、サービスの公開機能、攻撃者が到達できるアクセスを区別する。
主体の設定は攻撃者→`AttackerConfig`、被害者→`VictimConfig`、装置・サービス→`QpuServiceConfig`へ対応させる。通信・配置等の関係と、開始条件・training等の実験構成を分け、`ScenarioPolicy`は値ではなく統制を担う。
主体・関係・実験へ分ける方針は合意済みだが、具体的なfield、画面、API schema、根拠表示の方式は未確定。

検討順序は、共通フローの仮案を置き、複数の課題例で不足を確認し、
状態・時間・権限を具体化して最初の実装範囲を決める案へ更新した。
一課題への具体化は必要だが、共通フローの検討より先に全仕様を固定する条件にはしない。

## 状態の読み方

- **合意済み方針**: 会話でユーザーが明示または確認した方向性。実装完了の意味ではない。
- **具体化案**: 方針を実装へ落とす提案。スキーマや挙動は未確定。
- **未決定**: 必要性は認識しているが、選択・数値・契約が決まっていない。
- **実装済み**: [進捗](progress.md)にコードまたは固定記録の根拠があるもの。

## 基準版と文書管理

- 基準コミット: `699a110` (`Freeze physical model baseline v1`)
- 注釈タグ: `yuragi-strider-physical-baseline-v1`
- Freeze ID: `yuragi_strider_physical_baseline_v1`
- [物理モデル基準文書](../../../docs/validation/physical-model-baseline-v1.md)
- [全体の開発文書入口](../../README.md)

本フォルダは `.gitignore` の例外としてGit管理対象にしている。`docs_for_develop`の他の除外領域まで一括で追跡対象にはしない。
「同期」はこの時点の会話・コードを文書化したことを指し、自動同期機能ではない。
今後は決定時に `decisions.md` と `open-questions.md`、実装・検証時に `progress.md` を更新する。
