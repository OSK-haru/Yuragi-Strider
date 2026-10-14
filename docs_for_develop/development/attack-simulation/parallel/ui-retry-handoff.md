# UI再送安全性の引継ぎ

更新: 2026-10-04。UI intent／ローカル再送管理を実装・焦点検証済み。HTTP本接続は未接続。HTTP schema／terminal header採用を決定していない。

## 変更範囲

- `frontend/src/utils/attackWorkspace.ts`: `CommandRequestStore`、`sendCommandIntent`。新規操作ごとにUUIDを作り、未確定操作の手動再送には同じintentを使う。`programSubmitLine`は編集用の共通文法だけを生成し、IDは送信時のintentへ載せる。
- `frontend/src/types/attackSimulation.ts`: `CommandIntent={line,requestId}`を`executeCommand`に渡すUI接続口へ変更。応答は`confirmed`／`unknown`を区別する。これはHTTP schemaではない。
- `frontend/src/pages/AttackSimulatorPage.tsx`: UI生成・手入力とも同じ送信境界を通す。未確定時だけ既存端末に「同一要求を再送」を表示し、通常ボタンは「新規要求として送信」と表示する。新しい画面・コマンドは追加していない。
- `frontend/src/validation/attackWorkspace.test.mjs`: 応答喪失、新規と再送の識別、unknown、登録内容変更、保持上限、session終了、遅延応答の焦点回帰を追加。
- 本引継ぎファイル。

指定の5ファイルだけを編集した。開始時の既存変更／未追跡ファイルを保護し、API／core／設計正本は編集していない。依存追加、ネットワーク、秘密、Git履歴の変更はない。

## 実装済みの挙動

通常送信は同じ回路・同じ文字列でも新しいfallback ID。未確定要求の再送は元の文字列とIDを保持し、自動再試行はしない。既存の明示`--request-id`を含む文字列はそのまま渡す。その明示IDを優先する責務は共通backend parserにあり、新規送信ボタンでも利用者が明示したIDを書き換えない。

受付後の通信失敗／10秒の待機上限／unknownや不正形状の応答は未確定のまま保持する。`confirmed`はコマンド処理の応答が確認できたという意味で、job実行の成功ではない。unknownの応答内容を成功表示しない。最初の明確な受付前拒否は記録を解放するが、既に未確定だった再送の拒否は過去の受付を否定できないので記録を保持する。timeout後に遅れてtransportが成功しても未確定を黙って解除しない。

再送には同じsession・同じコマンド・同じ登録回路snapshotを要求する。編集だけでは登録回路を変えない。回路登録を試みた時点でもsnapshot revisionを進め、PUTの応答喪失を含めて同一IDの再送を止める。文法を再実装しないため、対象ファイルだけを解析して比較せず、登録workspace全体を保守的に比較する。同じ内容の再登録や他ファイルの登録でも再送を拒否する現実装。変更後は新規要求を明示的に選べる。ただし同じ明示`--request-id`を残せばbackendの内容衝突拒否を受けるため、本当に別実行にする場合は利用者がそのIDを変更／省略する。

保持はメモリ内の現在の1操作だけ。登録snapshotも16ファイル・各64 KiB・名前256 UTF-8 bytes以内。新規要求の明示選択で前の未確定操作を置き換えるので、警告にある通り別jobを作る可能性がある。履歴／永続再送台帳ではない。環境棄却の開始、環境成立、component unmount、別sessionへの操作で記録を消し、前環境の要求を復活させない。遅延した古い要求の結果も現在の記録を変更できない。

## 親への接続案（未承認）

現候補`POST /terminal`はbody `{line}`のみで、`TerminalSession.execute()`は明示ID省略時に呼出しごとUUIDを生成する。したがってintentを`{line}`だけへ落とすadapterは安全性を失う。現行terminalへそのまま接続してはならない。`POST /programs`には`Idempotency-Key`候補があるが、UIで共通文法を再解析してそちらへ転送する方式は採っていない。

最小の候補はterminal transportにもfallback keyを渡し、既存parserで解析した`program.submit`に限って`明示command.request_id → transport fallback → 省略時新規UUID`の順に解決すること。headerを使うかbody fieldを使うかは親のレビュー待ち。重複header・不正IDの拒否、明示IDとfallbackが異なる場合の明示ID優先、認可／principal scope／内容照合／session失効を既存Application APIへ通す検証が必要。UIが全コマンドにIDを付けるため、照会・取消等の非submitコマンドの文法／意味をこのfallbackによって変えないこと。

任意の手入力（引用、空白、option順、既存明示ID）も同じintent経由。UI側でshlexや共通grammarを全面再実装していない。backendはファイルを再読込みして同じIDの内容衝突を拒否する必要がある。別clientからのworkspace変更はUIだけでは検知できないため、その照合を省略できない。

今後のHTTP adapterはIDを確実に保持できるbackendへだけ接続し、公開投影を`confirmed`応答へ変換する。通信失敗・受付を証明できない応答は`AttackClientFailure(..., true)`／`unknown`、確実な受付前拒否だけを`uncertain=false`へ投影する。HTTP 2xxだけでjob実行成功とは扱わない。認証・管理者bootstrap・復旧契約の既存未決事項も引き続き親の担当。

## 検証

`frontend/`からローカル実行。依存インストール／server起動／networkなし。

- `node src/validation/attackWorkspace.test.mjs`: PASS、26件。ローカルfake clientで受付→応答喪失→同ID手動再送が1 job、通常の次実行が別jobになることを確認。明示IDの文字列保存を検査し、backendの実優先解決は未検証。
- `npm run lint`: PASS。
- `npm run build`: PASS。JS chunk 689.57 kB、500 kB超の警告あり。性能受入は未実施。

Reactの既存初期render検査はstatic markup。ブラウザのクリック／見た目、実HTTP、backendとUIの再送E2Eは未検証。HTTP本接続は未接続のまま。親はdiffとテストを再確認し、上記接続案を採用する場合にAPI/core側の検証を追加すること。
