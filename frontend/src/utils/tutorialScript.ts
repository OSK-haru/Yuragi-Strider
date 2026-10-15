/*
 * ナビペットが案内するチュートリアルの台本。
 *
 * 想定読者は量子力学を習っていない高校生。数式は出さず、
 * 「何が起きるか」と「画面のどこを見るか」だけを言う。
 * 専門語は初出でかならず言い換えを添える。
 *
 * コースは3本。1本目で回路を組んで動かし、2本目で条件を変えて
 * 結果がどう濁るかを実験する。3本目は攻撃シミュレーターで、Resetの漏れを
 * 攻撃者の立場から確かめる（台本は tutorialResetChallenge.ts）。
 */

import type { TutorialBeat, TutorialCourse, TutorialCourseId } from '../context/TutorialContextCore'
import { TUTORIAL_LONG_DURATION_US, TUTORIAL_SHORT_T1_US } from './tutorialProgress'
import { resetChallengeBeats } from './tutorialResetChallenge'

/* ============================================================================
   コース1: はじめての量子回路
   ========================================================================= */

const firstCircuitBeats: TutorialBeat[] = [
  /* --- 1. ようこそ ------------------------------------------------------ */
  {
    id: 'welcome-hello',
    chapter: 1,
    chapterTitle: 'ようこそ',
    route: 'home',
    mood: 'greet',
    text: 'やあ！ ぼくはナビペット。この装置の案内係だよ。ここに来てくれてありがとう！',
  },
  {
    id: 'welcome-plan',
    chapter: 1,
    chapterTitle: 'ようこそ',
    route: 'home',
    mood: 'explain',
    text: 'はじめてなら、少しだけ付き合ってほしいな。量子回路をひとつ自分で組んで、動かして、結果を読むところまで一緒にやってみよう。',
  },
  {
    id: 'welcome-controls',
    chapter: 1,
    chapterTitle: 'ようこそ',
    route: 'home',
    mood: 'nod',
    text: '進むのは「つぎへ」。戻りたくなったら「もどる」。やめたくなったら右上の「終了」でいつでも抜けられるよ。',
  },

  /* --- 2. このアプリは何か ---------------------------------------------- */
  {
    id: 'product-what',
    chapter: 2,
    chapterTitle: 'このアプリのこと',
    route: 'home',
    mood: 'explain',
    anchors: ['home-hero'],
    anchorLabel: 'このアプリの名前',
    text: 'このアプリ「YURAGI-STRIDER」は、量子コンピュータの中で起きていることを、計算で再現して画面に映す実験装置なんだ。',
  },
  {
    id: 'product-noise',
    chapter: 2,
    chapterTitle: 'このアプリのこと',
    route: 'home',
    mood: 'think',
    text: '本物の量子コンピュータは、とても壊れやすい。まわりの熱や磁気の影響で、計算の途中でも状態がじわじわ変わってしまうんだ。これがタイトルの「ゆらぎ」だよ。',
  },
  {
    id: 'product-value',
    chapter: 2,
    chapterTitle: 'このアプリのこと',
    route: 'home',
    mood: 'point',
    text: 'ここでは、その「ゆらぎ」まで物理の式にしたがって計算する。だから「理想の答え」と「現実に出てくる答え」を並べて見比べられるんだ。',
  },

  /* --- 3. 量子回路のしくみ ---------------------------------------------- */
  {
    id: 'circuit-bit',
    chapter: 3,
    chapterTitle: '量子回路のしくみ',
    route: 'home',
    mood: 'explain',
    text: 'ここで量子回路の話を少しだけ。ふつうのコンピュータのビットは 0 か 1 のどちらか。でも量子ビットは、0 と 1 が混ざった状態にもなれる。これを「重ね合わせ」って呼ぶよ。',
  },
  {
    id: 'circuit-gate',
    chapter: 3,
    chapterTitle: '量子回路のしくみ',
    route: 'home',
    mood: 'explain',
    text: 'その量子ビットを操作する道具が「ゲート」。たとえば H ゲートは、0 だった量子ビットを「0 と 1 が半分ずつ」の重ね合わせにする道具なんだ。',
  },
  {
    id: 'circuit-cnot',
    chapter: 3,
    chapterTitle: '量子回路のしくみ',
    route: 'home',
    mood: 'wonder',
    text: 'CNOT ゲートは 2 つの量子ビットをつなぐ道具。かたほう（制御側）が 1 のときだけ、もういっぽうをひっくり返す。「もし〜なら反転」というスイッチだね。',
  },
  {
    id: 'circuit-diagram',
    chapter: 3,
    chapterTitle: '量子回路のしくみ',
    route: 'home',
    mood: 'nod',
    text: '回路図は楽譜みたいなもの。横線が量子ビット1本ぶんで、左から右へ時間が流れる。その線の上にゲートを並べていくと、それが「プログラム」になるよ。',
  },

  /* --- 4. 回路エディターの案内 ------------------------------------------ */
  {
    id: 'studio-move',
    chapter: 4,
    chapterTitle: '回路をつくる場所',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'greet',
    text: '説明ばかりでもつまらないよね。IDEワークスペースへ移動したよ。まんなかの回路エディターが、回路を組み立てる作業台だよ。',
  },
  {
    id: 'studio-palette',
    chapter: 4,
    chapterTitle: '回路をつくる場所',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['gate-palette'],
    anchorLabel: 'ゲートの棚',
    text: 'ここが「パレット」。使えるゲートが種類ごとに並んでいる。色は仲間わけで、青は回転、紫は制御つき、といった具合だよ。',
  },
  {
    id: 'studio-canvas',
    chapter: 4,
    chapterTitle: '回路をつくる場所',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['circuit-canvas'],
    anchorLabel: '回路図',
    text: 'そしてこっちが回路図。横線が q0・q1 の 2 本ある。パレットでゲートを選んでマスをクリックするか、つまんで線の上に落とすと置けるよ。置いたゲートはドラッグで動かせて、選んで Delete で消せる。',
  },

  /* --- 5. ベル回路を組む ------------------------------------------------ */
  {
    id: 'bell-goal',
    chapter: 5,
    chapterTitle: 'ベル回路をつくる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'explain',
    text: '最初の目標は「ベル状態」をつくる回路。2 つの量子ビットが、どれだけ離れていても結果を共有してしまう状態…「量子もつれ」のいちばんシンプルな形だよ。',
  },
  {
    id: 'bell-place-h',
    chapter: 5,
    chapterTitle: 'ベル回路をつくる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['gate-H', 'circuit-canvas'],
    anchorLabel: '手順1: H を置く',
    text: '手順1。パレットの「H」を選んで、いちばん上の線（q0）のいちばん左のマスをクリック（ドラッグして落としてもOK）。q0 が 0 と 1 の重ね合わせになるよ。',
    waitFor: 'h-placed',
    waitingHint: 'H を置くのを待っているよ。',
    autoAdvance: true,
  },
  {
    id: 'bell-place-cnot',
    chapter: 5,
    chapterTitle: 'ベル回路をつくる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['gate-CNOT', 'circuit-canvas'],
    anchorLabel: '手順2: CNOT を置く',
    text: 'いいね！ 手順2は CNOT。パレットの「CNOT」を押してから、H の右となりの列で q0 → q1 の順にクリックすると、2 本の線がつながるよ。',
    waitFor: 'bell-ready',
    waitingHint: 'q0 を制御、q1 を相手にした CNOT を待っているよ。',
    autoAdvance: true,
  },
  {
    id: 'bell-done',
    chapter: 5,
    chapterTitle: 'ベル回路をつくる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'cheer',
    anchors: ['circuit-canvas'],
    anchorLabel: '完成したベル回路',
    text: 'できた！ これがベル回路。理想的にはこの回路を測ると「00」か「11」だけが半々で出て、「01」や「10」は出てこない。2 つの量子ビットの答えがそろってしまうんだ。',
  },

  /* --- 6. 実行する ------------------------------------------------------ */
  {
    id: 'run-move',
    chapter: 6,
    chapterTitle: '走らせてみる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'greet',
    text: '組んだ回路を実際に走らせてみよう。スタートボタンは上のツールバーにあるよ。細かい設定は今のままで大丈夫。',
  },
  {
    id: 'run-press',
    chapter: 6,
    chapterTitle: '走らせてみる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['run-button'],
    anchorLabel: 'ここを押す',
    text: 'この「シミュレーションを実行」を押してみて！ 量子ビットの状態を、ごく短い時間ずつ進めながら計算していくよ。',
    waitFor: 'simulation-finished',
    waitingHint: '実行の完了を待っているよ。',
    autoAdvance: true,
  },
  {
    id: 'run-finished',
    chapter: 6,
    chapterTitle: '走らせてみる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'cheer',
    anchors: ['run-button'],
    anchorLabel: '計算おわり',
    text: '終わった！ 計算そのものは一瞬。でも、この中では「ゲートをかけている間もノイズが効いている」という、けっこう真面目な物理が回っているんだ。',
  },

  /* --- 7. 結果を読む ---------------------------------------------------- */
  {
    id: 'explorer-move',
    chapter: 7,
    chapterTitle: '結果を読む',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'greet',
    text: '結果を見に行こう。まんなかを「状態エクスプローラー」に切り替えたよ。計算の途中で量子ビットがどうなっていたかを、いろいろな角度から覗ける部屋だよ。',
  },
  {
    id: 'explorer-timeline',
    opensPanel: 'metric-timeline',
    chapter: 7,
    chapterTitle: '結果を読む',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'point',
    anchors: ['metric-timeline'],
    anchorLabel: '指標タイムライン',
    text: '「指標タイムライン」を開いておいたよ。横軸が時間、縦の線が回路の中の出来事。ここで 2 つの数字の移り変わりを追いかける。',
  },
  {
    id: 'explorer-fidelity',
    opensPanel: 'metric-timeline',
    chapter: 7,
    chapterTitle: '結果を読む',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'explain',
    anchors: ['metric-timeline'],
    anchorLabel: '忠実度の線',
    text: 'ひとつめは「忠実度（フィデリティ）」。ノイズがまったくない理想の状態と、どれくらい同じかを 0〜1 で表した点数だよ。1.00 なら完璧、下がるほど理想からずれたということ。',
  },
  {
    id: 'explorer-purity',
    opensPanel: 'metric-timeline',
    chapter: 7,
    chapterTitle: '結果を読む',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'explain',
    anchors: ['metric-timeline'],
    anchorLabel: '純度の線',
    text: 'ふたつめは「純度（ピュリティ）」。量子ビットの状態が、どれだけ「はっきりしているか」の目安。まわりの環境と混ざってしまうほど下がっていく。',
  },
  {
    id: 'explorer-decay',
    opensPanel: 'metric-timeline',
    chapter: 7,
    chapterTitle: '結果を読む',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'wonder',
    anchors: ['metric-timeline'],
    anchorLabel: '右へ行くほど下がる',
    text: '線が右へ行くほど下がっているのが分かるかな。これが「ゆらぎ」の正体だよ。時間が経つほど、量子ビットは環境に情報を奪われていく。だから回路が長いほど答えが濁るんだ。',
  },
  {
    id: 'explorer-more',
    chapter: 7,
    chapterTitle: '結果を読む',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'nod',
    anchors: ['explorer-panel-menu'],
    anchorLabel: '表示項目',
    text: 'ほかにも Bloch 球で状態の向きを見たり、密度行列で全体を眺めたりできる。見たいパネルはこの「表示項目」から選べるよ。',
  },

  /* --- 8. さようなら ---------------------------------------------------- */
  {
    id: 'farewell-freedom',
    chapter: 8,
    chapterTitle: 'いってらっしゃい',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'greet',
    text: 'これで一周おしまい！ あとは自由に触ってみて。ゲートを増やす、量子ビットを増やす…どれをやっても、結果はちゃんと物理に従って変わるよ。',
  },
  {
    id: 'farewell-bye',
    chapter: 8,
    chapterTitle: 'いってらっしゃい',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'farewell',
    text: 'もっと知りたくなったら、ホームの2本目「ゆらぎ実験」もどうぞ。条件を変えて、答えがどれだけ濁るかを自分の手で確かめられるよ。それじゃあ、またね！',
  },
]

/* ============================================================================
   コース2: ゆらぎ実験（パラメーターを変えて比べる）
   ========================================================================= */

const noiseParameterBeats: TutorialBeat[] = [
  /* --- 1. ねらい -------------------------------------------------------- */
  {
    id: 'noise-hello',
    chapter: 1,
    chapterTitle: '今日の実験',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'greet',
    text: 'おかえり！ こんどは実験だよ。回路はそのままで、量子ビットの「置かれている条件」だけを変える。それだけで答えがどれくらい濁るのかを、自分の目で確かめよう。',
  },
  {
    id: 'noise-method',
    chapter: 1,
    chapterTitle: '今日の実験',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'explain',
    text: '進め方は理科の実験と同じ。まず「そのまま」で1回動かして基準をとる。つぎに条件をひとつだけ変えてもう1回。変えたのは1つだけだから、差が出たらその条件のせいだと言えるよね。',
  },

  /* --- 2. 回路の確認 ---------------------------------------------------- */
  {
    id: 'noise-circuit',
    chapter: 2,
    chapterTitle: '回路の確認',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['circuit-summary'],
    anchorLabel: 'いまの回路',
    text: 'まず材料の確認。実験にはゲートの入った回路が要るよ。左の「回路」欄に今の回路の大きさが出ている。空っぽなら、まんなかの回路エディターで H を1つ置くだけでもいい。',
    waitFor: 'circuit-ready',
    waitingHint: 'ゲートが1つ以上ある回路を待っているよ。',
    autoAdvance: true,
  },

  /* --- 3. パラメーターを見る -------------------------------------------- */
  {
    id: 'noise-open-settings',
    opensPanel: 'advanced-settings',
    chapter: 3,
    chapterTitle: '条件をひらく',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['advanced-settings'],
    anchorLabel: '詳細設定',
    text: '条件は右の「計算パラメーター」欄にあるよ。ふだんは気にしなくて大丈夫な場所だけど、今日はここが主役。閉じていたら開いておくね。',
  },
  {
    id: 'noise-panel-tour',
    opensPanel: 'advanced-settings',
    chapter: 3,
    chapterTitle: '条件をひらく',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'explain',
    anchors: ['parameter-panel'],
    anchorLabel: 'パラメーター',
    text: 'ここが実験装置のつまみ。大きく3つ。「デバイス」は量子ビットそのものの性能、「環境」は置かれている場所の状態、「シミュレーション時間」はどれだけの長さを計算するか、だよ。',
  },
  {
    id: 'noise-t1-meaning',
    opensPanel: 'advanced-settings',
    chapter: 3,
    chapterTitle: '条件をひらく',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'wonder',
    anchors: ['param-t1_max_us'],
    anchorLabel: 'T1',
    text: '今日いじるのは「最大 T1」。T1 は、1 になっている量子ビットが、力尽きて 0 に落ちてしまうまでのだいたいの時間なんだ。長いほど丈夫な量子ビット、短いほどすぐ壊れる。',
  },

  /* --- 4. 基準をとる ---------------------------------------------------- */
  {
    id: 'noise-baseline-run',
    opensPanel: 'advanced-settings',
    chapter: 4,
    chapterTitle: '基準をとる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['run-button'],
    anchorLabel: 'まずはそのまま実行',
    text: 'まずは何も変えずに1回。この結果が比べるときの物差しになるよ。「シミュレーションを実行」を押して。',
    waitFor: 'simulation-finished',
    waitingHint: '1回目の実行を待っているよ。',
    autoAdvance: true,
  },
  {
    id: 'noise-baseline-result',
    opensPanel: 'advanced-settings',
    chapter: 4,
    chapterTitle: '基準をとる',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'cheer',
    showRunSamples: true,
    text: '1回目の記録をとったよ。左下に出しておくね。この忠実度が今日の基準。ここからどれだけ下がるかを見ていこう。',
  },

  /* --- 5. T1 を短くする ------------------------------------------------- */
  {
    id: 'noise-lower-t1',
    opensPanel: 'advanced-settings',
    chapter: 5,
    chapterTitle: '弱い量子ビットにする',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['param-t1_max_us'],
    anchorLabel: `${TUTORIAL_SHORT_T1_US} 以下まで下げる`,
    text: `では条件をひとつだけ変えよう。「最大 T1」のつまみを ${TUTORIAL_SHORT_T1_US} μs 以下まで回してみて。数字を直接クリックしても打ち込めるよ。すぐ力尽きる、弱い量子ビットにするということ。`,
    waitFor: 't1-lowered',
    waitingHint: `最大 T1 を ${TUTORIAL_SHORT_T1_US} μs 以下にするのを待っているよ。`,
    autoAdvance: true,
  },
  {
    id: 'noise-run-short-t1',
    opensPanel: 'advanced-settings',
    chapter: 5,
    chapterTitle: '弱い量子ビットにする',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['run-button'],
    anchorLabel: 'もう一度実行',
    text: '回路は1文字も変えていないよ。変えたのは量子ビットの丈夫さだけ。この状態でもう一度実行してみて。',
    waitFor: 'short-t1-run-finished',
    waitingHint: '2回目の実行を待っているよ。',
    autoAdvance: true,
  },
  {
    id: 'noise-compare-t1',
    opensPanel: 'advanced-settings',
    chapter: 5,
    chapterTitle: '弱い量子ビットにする',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'wonder',
    showRunSamples: true,
    text: '見比べてみて。同じ回路・同じ手順なのに、忠実度が下がったよね。量子ビットが弱くなったぶんだけ、計算しているあいだに答えが環境へ漏れていったんだ。',
  },

  /* --- 6. 時間を延ばす -------------------------------------------------- */
  {
    id: 'noise-time-idea',
    opensPanel: 'advanced-settings',
    chapter: 6,
    chapterTitle: '時間を延ばす',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'explain',
    text: 'ゆらぎには、もうひとつ大事な相手がいる。時間だよ。ノイズは一瞬で効くのではなく、置かれている間じゅうずっと効き続ける。ということは…長く置くほど濁るはずだよね。',
  },
  {
    id: 'noise-extend-duration',
    opensPanel: 'advanced-settings',
    chapter: 6,
    chapterTitle: '時間を延ばす',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['param-duration_us'],
    anchorLabel: `${TUTORIAL_LONG_DURATION_US} 以上にする`,
    text: `「総シミュレーション時間」を ${TUTORIAL_LONG_DURATION_US} μs 以上に増やしてみて。回路が終わったあとも、量子ビットをその場に置いておく時間が延びるんだ。`,
    waitFor: 'duration-extended',
    waitingHint: `総シミュレーション時間を ${TUTORIAL_LONG_DURATION_US} μs 以上にするのを待っているよ。`,
    autoAdvance: true,
  },
  {
    id: 'noise-run-long',
    opensPanel: 'advanced-settings',
    chapter: 6,
    chapterTitle: '時間を延ばす',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'point',
    anchors: ['run-button'],
    anchorLabel: '3回目の実行',
    text: 'これで3回目。弱い量子ビットを、さらに長い時間そのまま置いておくとどうなるか。実行してみて。',
    waitFor: 'long-run-finished',
    waitingHint: '3回目の実行を待っているよ。',
    autoAdvance: true,
  },
  {
    id: 'noise-compare-long',
    opensPanel: 'advanced-settings',
    chapter: 6,
    chapterTitle: '時間を延ばす',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'think',
    showRunSamples: true,
    text: '3つ並んだね。下へ行くほど忠実度が落ちているはず。「弱いほど濁る」「長いほど濁る」…この2つが、量子コンピュータを作るのが難しい理由そのものなんだ。',
  },

  /* --- 7. 曲線で確かめる ------------------------------------------------ */
  {
    id: 'noise-explorer',
    opensPanel: 'metric-timeline',
    chapter: 7,
    chapterTitle: '崩れ方を見る',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'point',
    anchors: ['metric-timeline'],
    anchorLabel: '指標タイムライン',
    text: '最後の実行の中身を見てみよう。指標タイムラインを開いておいたよ。数字ひとつではなく、時間に沿ってどう崩れていったかが線で見える。',
  },
  {
    id: 'noise-explorer-slope',
    opensPanel: 'metric-timeline',
    chapter: 7,
    chapterTitle: '崩れ方を見る',
    route: 'gate-aware-ide',
    ideView: 'explorer',
    mood: 'explain',
    anchors: ['metric-timeline'],
    anchorLabel: '傾きが答え',
    text: '大事なのは線の「傾き」だよ。T1 を短くすると、この下がり方が急になる。最後に残った値だけでなく、どれくらいの速さで崩れるかが量子ビットの性能そのものなんだ。',
  },
  {
    id: 'noise-others',
    opensPanel: 'advanced-settings',
    chapter: 7,
    chapterTitle: '崩れ方を見る',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'nod',
    anchors: ['parameter-panel'],
    anchorLabel: 'ほかのつまみ',
    text: 'ほかのつまみも同じように試せるよ。「最大 Tφ」は位相のずれやすさ、「温度」を上げると熱で勝手に励起され、「デバイス品質」を下げると全体がまとめて悪くなる。どれも独立に効くよ。',
  },

  /* --- 8. さようなら ---------------------------------------------------- */
  {
    id: 'noise-farewell-tip',
    chapter: 8,
    chapterTitle: 'いってらっしゃい',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'greet',
    text: 'コツをひとつ。変えるつまみは毎回ひとつだけ。2つ同時に動かすと、どっちが効いたのか分からなくなる。これは量子でも、ほかのどんな実験でも同じだよ。',
  },
  {
    id: 'noise-farewell-bye',
    chapter: 8,
    chapterTitle: 'いってらっしゃい',
    route: 'gate-aware-ide',
    ideView: 'circuit',
    mood: 'farewell',
    text: '実験おつかれさま！ 元の設定に戻したいときは、ページを読み込み直せば既定値に戻るよ。それじゃあ、いい実験を！',
  },
]

/* ========================================================================= */

export const tutorialCourses: Record<TutorialCourseId, TutorialCourse> = {
  'first-circuit': {
    id: 'first-circuit',
    title: 'はじめての量子回路',
    summary:
      '量子ビットとゲートの話から、ベル回路を自分で組んで動かし、結果の読み方まで。予備知識はいりません。',
    duration: '約4分',
    beats: firstCircuitBeats,
  },
  'noise-parameters': {
    id: 'noise-parameters',
    title: 'ゆらぎ実験',
    summary:
      '回路はそのままで条件だけを変え、答えがどれだけ濁るかを3回の実行で比べます。1本目のあとにどうぞ。',
    duration: '約3分',
    beats: noiseParameterBeats,
  },
  'reset-challenge': {
    id: 'reset-challenge',
    title: 'Resetの漏れを突く',
    summary:
      '共有された量子コンピュータで、前の利用者の bit が Reset のあとにも残ることを、攻撃者の立場で確かめます。1 bit の課題を、判定ルールの提出まで通します。',
    duration: '約8分',
    beats: resetChallengeBeats,
  },
}

export const tutorialCourseList: TutorialCourse[] = [
  tutorialCourses['first-circuit'],
  tutorialCourses['noise-parameters'],
  tutorialCourses['reset-challenge'],
]

export function tutorialChapterCount(course: TutorialCourse): number {
  return course.beats.reduce((highest, beat) => Math.max(highest, beat.chapter), 0)
}
