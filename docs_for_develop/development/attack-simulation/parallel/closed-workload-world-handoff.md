# 閉じた有限sourceの共通時間・実行接続

2026-10-05。前段は[物理確定境界](declared-physical-commit-handoff.md)。証拠は[進捗](../progress.md)、時間契約は[実行境界](../runtime-boundaries-and-resolutions.md)を正本とする。

## 実装範囲

`api/attack_world.py`の`ClosedWorkloadWorld`が、同じsessionの凍結workload runnerと物理executorを一度だけ所有する。`start()`で時計を開始し、上限必須の`pump(max_transitions=...)`でnominal target同期、共通受付、FIFO headのwindow生成、START、FINISH、共通自己結果の公開と後続操作生成を時刻順に接続する。手動window／seal／release callbackは不要。timer／HTTP／UIの実運用loopではなく、callerがbounded pumpを呼ぶ内部adapter。

serviceの`declared_service_timing`は成立前manifestに`processing_delay_us`、`publication_delay_us`、`denial_delay_us`、非空`basis`を明示する。省略や不正field／負値は拒否し、実provider数値の既定値を設けない。clock domainは凍結world clockと一致させる。Program・actorの宣言通信durationと、実ホスト通信は別である。モデル係数・最初の課題presetを採用した意味ではない。

headのSTARTはarrival＋処理durationと、直前の確定FINISHから純粋に求める。物理durationは共通コンパイル命令・shot・測定group・実tenant交代Resetから生成する。公開待ちはQPUを占有しない。`begin_declared_next()`でSTARTとRUNNINGをFINISH到達より先に確定し、ホスト数値計算の速さでqueued取消区間を延長しない。

## 網羅性・因果依存の証明範囲

対象sourceは成立前に予約した有限submit-only workloadだけ。runnerをexclusive claimして手動進行を封じ、sessionのraw HTTP／未登録到着は引き続き拒否する。現在headの後続は登録release以後、後続queued jobの自己結果はhead FINISH以後という下界を使う。共通bookの既存result barrierを単調にtightenし、同じstateの再公開でも下界を失わない。result依存そのものを消したり、物理ticketへ無条件に変換したりしない。

残るreservation／result barrierを越さない位置だけadmission cutoffとphysical sealを確定する。seal更新は既存の生存session／資格情報の短いtransition境界を通し、registry破棄後のmetadata変更を拒否する。純粋workloadのゼロdelay依存も、自己arrivalより後の同着orderを下界とする。源泉が未知のlive watermarkを空queueやhost完了から推測した証明ではない。

この下界はsubmit-only capabilityに限る。live取消・失敗返信を追加するなら、その到着・応答時刻・依存差替えも同じownerへ接続してから再証明する。未登録job、外部からの成功、未対応control／failure terminal遷移は基盤異常として止め、過去の返信を捏造しない。自由／課題modeで同じadapterを使い、製品全体の取消能力を廃止したわけではない。

## 失敗・精度・資源

失われたadmission応答は残存grantの同一要求再送をqueue検査より先に行う。FINISH確定後の応答喪失は、executorの確定位置から次windowを求め、独立した継続位置cacheへ依存しない。START／数値中断は既存確定証跡を保持して明示retryへ渡し、二重job・追加Reset・物理failureへ変えない。

長い経過後のfloat絶対座標は、小数durationを引算すると丸め誤差が増える。adapterのFINISHは`start + compiled duration`の丸め座標と照合し、物理dtはコンパイル値のまま使う。正durationが完全に消える座標やoverflowはSTART前に拒否する。float metadataの有限精度をなくした保証・固定小数点clockの実装ではない。

pending／履歴／job／event batch／matrix-workは既存の明示上限を保つ。pumpは一回最大16384 transitionまでで、実呼出し上限を要求する。上限到達は未処理量として返し、履歴満杯等は実ownerがcallback前に基盤容量不足を報告する。自動retry・sleep・無制限catch-upを加えない。pump単位数を数値計算時間上限や実RSS保証とは扱わない。

## 分担と確認

- timing担当: 新規`declared_service_timing.py`と対応19試験。共通durationの独立生成、FIFO・ゼロduration・overflow。
- 親: world adapter、book依存下界、runner所有／seal、START境界と座標精度修正、統合試験と文書。
- 独立監査: 応答喪失後のadmission再送、FINISH後継続位置の二指摘を再現。timing担当の狭い再点検でも長時間座標精度の指摘を再現。親が修正し回帰試験を追加。件数と再監査範囲は進捗正本を参照。

新規受入は両mode、両主体、複数qubit、300qubit metadata＋3qubit retained領域、自己結果反復・ゼロdelay同着、START先行／公開待ち、数値遅延・中断、応答喪失、capacity、破棄、再送を含む。fixtureの無限T1等は隔離した試験条件で、製品presetではない。

## 次の接続

live HTTP/control到着の同期と安全sealへの登録、実所有者の成立／破棄・同一process復旧、共通UIの実client接続、旧Reset課題の共通Program／予算移行が残る。service timingの根拠表示、実provider／最初の被害者列・物理係数の採用も別に行う。UI・ブラウザ・公開認証・実ネットワーク・長時間RSS・provider現実性は今回検証していない。追加依存、network有効化、secret処理、Git変更、commit／push／deployなし。
