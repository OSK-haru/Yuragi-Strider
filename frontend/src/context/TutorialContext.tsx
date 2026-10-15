import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { NavigationRoute } from '../components/AppNavigation'
import { tutorialChapterCount, tutorialCourses } from '../utils/tutorialScript'
import {
  TutorialContext,
  type TutorialCondition,
  type TutorialContextValue,
  type TutorialCourseId,
  type TutorialRunSample,
} from './TutorialContextCore'

const STORAGE_KEY = 'yuragi_strider:tutorial-completed'
/* 条件が満たされてから自動で次へ進むまでの間。読む時間を少し残す。 */
const AUTO_ADVANCE_DELAY_MS = 1200

const emptyConditions: Record<TutorialCondition, boolean> = {
  'circuit-ready': false,
  'h-placed': false,
  'bell-ready': false,
  'simulation-finished': false,
  't1-lowered': false,
  'short-t1-run-finished': false,
  'duration-extended': false,
  'long-run-finished': false,
  'attack-mode-challenge': false,
  'attack-challenge-selected': false,
  'attack-challenge-env': false,
  'attack-measure-ready': false,
  'attack-ref0-submitted': false,
  'attack-ref1-submitted': false,
  'attack-result-seen': false,
  'attack-victim-submitted': false,
  'attack-answered': false,
}

type TutorialProviderProps = {
  currentRoute: NavigationRoute
  onNavigate: (route: NavigationRoute) => void
  children: ReactNode
}

/*
 * 修了したコースの記録。1本目だけを終えた人と両方終えた人を区別したいので、
 * 真偽値ではなく id の一覧で持つ。読めない値が入っていても黙って捨てる。
 */
function readCompletedCourses(): TutorialCourseId[] {
  if (typeof window === 'undefined') {
    return []
  }

  const stored = window.localStorage.getItem(STORAGE_KEY)
  if (stored === null) {
    return []
  }

  /* 旧版は 'yes' の1語だけを書いていた。1本目の修了として引き継ぐ。 */
  if (stored === 'yes') {
    return ['first-circuit']
  }

  return stored
    .split(',')
    .filter((id): id is TutorialCourseId => (
      id === 'first-circuit' || id === 'noise-parameters' || id === 'reset-challenge'
    ))
}

export function TutorialProvider({ currentRoute, onNavigate, children }: TutorialProviderProps) {
  const [courseId, setCourseId] = useState<TutorialCourseId | null>(null)
  const [beatIndex, setBeatIndex] = useState(0)
  const [completedCourses, setCompletedCourses] = useState<TutorialCourseId[]>(readCompletedCourses)
  const [conditions, setConditions] = useState<Record<TutorialCondition, boolean>>(emptyConditions)
  const [runSamples, setRunSamples] = useState<TutorialRunSample[]>([])
  /*
   * 画面移動と自動進行は「進んだとき」だけ起こしたい。
   * 利用者が自分でページを移した直後に連れ戻したり、
   * 「もどる」で戻った先からすぐ弾き出したりしないよう、
   * 直前の操作方向と現在地を ref で持ち、依存配列から外す。
   */
  const routeRef = useRef(currentRoute)
  const navigateRef = useRef(onNavigate)
  const wentBackRef = useRef(false)

  /* 参照だけを毎描画後に最新へ入れ替える。並び順の都合で、この効果は先頭に置く。 */
  useEffect(() => {
    routeRef.current = currentRoute
    navigateRef.current = onNavigate
  })

  const course = courseId === null ? null : tutorialCourses[courseId]
  const beats = course?.beats ?? []
  const beat = course === null ? null : beats[beatIndex] ?? null
  const isBeatSatisfied = beat === null || beat.waitFor === undefined || conditions[beat.waitFor]

  const markCompleted = useCallback((completedId: TutorialCourseId) => {
    setCompletedCourses((current) => (
      current.includes(completedId) ? current : [...current, completedId]
    ))
  }, [])

  /*
   * 保存は更新関数の外で行う。StrictMode は更新関数を2回呼ぶので、
   * その中に副作用を置いてはいけない。未修了は既定値なので書かない。
   */
  useEffect(() => {
    if (completedCourses.length === 0) {
      return
    }

    window.localStorage.setItem(STORAGE_KEY, completedCourses.join(','))
  }, [completedCourses])

  const start = useCallback((nextCourseId: TutorialCourseId) => {
    wentBackRef.current = false
    setBeatIndex(0)
    /*
     * 達成状況と実行記録は毎回まっさらから。前に走らせた結果を根拠に
     * 「実行してみて」の場面を飛ばしてしまわないようにする。
     * 回路や設定から読む条件は、該当ページに入った時点で報告し直される。
     */
    setConditions(emptyConditions)
    setRunSamples([])
    setCourseId(nextCourseId)
  }, [])

  const exit = useCallback(() => {
    setCourseId(null)
  }, [])

  /* 最後まで進んだら、記録を残して静かに閉じる。 */
  const next = useCallback(() => {
    wentBackRef.current = false

    if (courseId === null) {
      return
    }

    if (beatIndex >= tutorialCourses[courseId].beats.length - 1) {
      markCompleted(courseId)
      setCourseId(null)
      return
    }

    setBeatIndex(beatIndex + 1)
  }, [beatIndex, courseId, markCompleted])

  const back = useCallback(() => {
    wentBackRef.current = true
    setBeatIndex((current) => Math.max(0, current - 1))
  }, [])

  const reportCondition = useCallback((condition: TutorialCondition, met: boolean) => {
    setConditions((current) => (current[condition] === met ? current : { ...current, [condition]: met }))
  }, [])

  /* 案内していない間の実行は記録しない。比較表はコースの中の話。 */
  const recordRun = useCallback((sample: Omit<TutorialRunSample, 'id'>) => {
    if (courseId === null) {
      return
    }

    setRunSamples((current) => [...current, { ...sample, id: current.length + 1 }])
  }, [courseId])

  /* ビートが変わったら、そのビートの画面へ連れて行く。 */
  useEffect(() => {
    if (courseId === null) {
      return
    }

    const activeBeat = tutorialCourses[courseId].beats[beatIndex]
    if (activeBeat && activeBeat.route !== routeRef.current) {
      navigateRef.current(activeBeat.route)
    }
  }, [courseId, beatIndex])

  /* 待機条件が満たされたら、少し置いて自分から次へ進む。 */
  useEffect(() => {
    if (courseId === null || wentBackRef.current) {
      return
    }

    const activeBeat = tutorialCourses[courseId].beats[beatIndex]
    if (!activeBeat?.autoAdvance || activeBeat.waitFor === undefined) {
      return
    }
    if (!conditions[activeBeat.waitFor]) {
      return
    }

    const timeoutId = window.setTimeout(() => next(), AUTO_ADVANCE_DELAY_MS)
    return () => window.clearTimeout(timeoutId)
  }, [courseId, beatIndex, conditions, next])

  /* どの画面にいても、Escape で必ず抜けられるようにしておく。 */
  useEffect(() => {
    if (courseId === null) {
      return
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setCourseId(null)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [courseId])

  const value = useMemo<TutorialContextValue>(
    () => ({
      isActive: course !== null,
      completedCourses,
      course,
      beat,
      beatIndex,
      beatCount: beats.length,
      chapterCount: course === null ? 0 : tutorialChapterCount(course),
      isBeatSatisfied,
      canGoBack: beatIndex > 0,
      isLastBeat: beatIndex >= beats.length - 1,
      runSamples,
      start,
      next,
      back,
      exit,
      reportCondition,
      recordRun,
    }),
    [
      course,
      completedCourses,
      beat,
      beatIndex,
      beats.length,
      isBeatSatisfied,
      runSamples,
      start,
      next,
      back,
      exit,
      reportCondition,
      recordRun,
    ],
  )

  return <TutorialContext.Provider value={value}>{children}</TutorialContext.Provider>
}
