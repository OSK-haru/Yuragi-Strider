import { createContext } from 'react'
import type { NavigationRoute } from '../components/AppNavigation'

/*
 * チュートリアルの型と文脈。ナビペットの会話1コマを「ビート」と呼ぶ。
 * 章（chapter）は利用者に見せる区切りで、ビートはその中の1セリフ。
 * 複数の「コース」を持ち、ホームから選んで始める。
 */

/* ペットの表情と動き。見た目だけの指定で、進行には影響しない。 */
export type PetMood =
  | 'greet'
  | 'explain'
  | 'think'
  | 'point'
  | 'cheer'
  | 'wonder'
  | 'nod'
  | 'wait'
  | 'farewell'

/*
 * 画面側から報告してもらう達成条件。
 * イベントではなく「いま満たされているか」を持つので、
 * 何度報告されても、戻ってきても同じ判定になる。
 *
 * 「〜-run-finished」だけは実行の完了そのものを指す。こちらは
 * 実行時の設定を条件に含めることで、前の実行を使い回せないようにしている。
 */
export type TutorialCondition =
  /* 回路に少なくとも1つゲートが置かれている。 */
  | 'circuit-ready'
  | 'h-placed'
  | 'bell-ready'
  /* 設定を変えずに1回実行した（基準の実行）。 */
  | 'simulation-finished'
  /* T1 を目安まで短くした。 */
  | 't1-lowered'
  /* T1 を短くした状態で実行し終えた。 */
  | 'short-t1-run-finished'
  /* 総シミュレーション時間を目安まで延ばした。 */
  | 'duration-extended'
  /* 時間を延ばした状態で実行し終えた。 */
  | 'long-run-finished'
  /*
   * 攻撃シミュレーター（Reset課題）。画面が報告する。
   * 「〜-submitted」「〜-seen」「〜-answered」は出来事なので、コースを始めるたびに
   * まっさらから数え直す。それ以外は、いま満たされているかを毎回報告する。
   */
  | 'attack-mode-challenge'
  | 'attack-challenge-selected'
  | 'attack-challenge-env'
  | 'attack-measure-ready'
  | 'attack-ref0-submitted'
  | 'attack-ref1-submitted'
  | 'attack-result-seen'
  | 'attack-victim-submitted'
  | 'attack-answered'

export type TutorialBeat = {
  id: string
  /* 章番号（1始まり）と、進捗表示に出す章の名前。 */
  chapter: number
  chapterTitle: string
  /* このビートを表示する画面。前のビートと違うときだけ移動する。 */
  route: NavigationRoute
  /* IDE画面での作業領域の表示。省略時は利用者の選んだ表示のまま。 */
  ideView?: 'circuit' | 'explorer'
  /* 攻撃シミュレーターの画面（環境設定／攻撃者ワークスペース）。省略時は今のまま。 */
  attackView?: 'setup' | 'workspace'
  mood: PetMood
  text: string
  /* 暗転から外して強調する要素の data-tutorial-anchor 値。 */
  anchors?: string[]
  /* 強調枠に添える短いラベル。 */
  anchorLabel?: string
  /* この条件が満たされるまで「つぎへ」を待機表示にする。 */
  waitFor?: TutorialCondition
  /* 待機中に出す補足。 */
  waitingHint?: string
  /* 条件が満たされたら自動で次へ進む。 */
  autoAdvance?: boolean
  /* 実行結果の比較表を横に出す。 */
  showRunSamples?: boolean
  /*
   * 話に必要な折りたたみを開いた状態にする。
   * 「まず自分で開いて」と言うと話の腰が折れるので、画面側が面倒を見る。
   */
  opensPanel?: 'advanced-settings' | 'metric-timeline'
}

export type TutorialCourseId = 'first-circuit' | 'noise-parameters' | 'reset-challenge'

export type TutorialCourse = {
  id: TutorialCourseId
  /* ホームの入口と進行HUDに出す名前。 */
  title: string
  /* ホームの入口に出す一行説明。 */
  summary: string
  /* 所要時間の目安。 */
  duration: string
  beats: TutorialBeat[]
}

/* 実行1回ぶんの記録。設定を変えた前後を並べて見せるために貯める。 */
export type TutorialRunSample = {
  id: number
  fidelity: number | null
  purity: number | null
  t1Us: number
  durationUs: number
}

export type TutorialContextValue = {
  isActive: boolean
  /* 一度でも最後まで見たコース。ホームの誘導表示に使う。 */
  completedCourses: TutorialCourseId[]
  course: TutorialCourse | null
  beat: TutorialBeat | null
  beatIndex: number
  beatCount: number
  chapterCount: number
  /* 現在のビートの待機条件が満たされているか。条件なしなら常に true。 */
  isBeatSatisfied: boolean
  canGoBack: boolean
  isLastBeat: boolean
  runSamples: TutorialRunSample[]
  start: (courseId: TutorialCourseId) => void
  next: () => void
  back: () => void
  exit: () => void
  /* 画面側から達成条件の現状を報告する。 */
  reportCondition: (condition: TutorialCondition, met: boolean) => void
  /* 実行が終わるたび、比較用に結果を1件足す。 */
  recordRun: (sample: Omit<TutorialRunSample, 'id'>) => void
}

export const TutorialContext = createContext<TutorialContextValue | null>(null)
