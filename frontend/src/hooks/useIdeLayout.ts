import { useCallback, useEffect, useState } from 'react'

/*
 * IDE 画面のペインの表示状態と大きさ。幅と表示状態は画面ごとにブラウザへ
 * 保存する（保存できない環境でも初期値で動く）。描画は components/IdeWorkbench。
 */

export type PaneKey = 'navigator' | 'toolbox' | 'inspector' | 'messages'

export type IdeLayoutState = {
  navigatorWidth: number
  toolboxWidth: number
  inspectorWidth: number
  messagesHeight: number
  visible: Record<PaneKey, boolean>
}

const DEFAULT_LAYOUT: IdeLayoutState = {
  navigatorWidth: 220,
  toolboxWidth: 340,
  inspectorWidth: 300,
  messagesHeight: 200,
  visible: { navigator: true, toolbox: true, inspector: true, messages: true },
}

export const LIMITS = {
  navigatorWidth: [140, 520],
  toolboxWidth: [220, 720],
  inspectorWidth: [200, 640],
  messagesHeight: [80, 640],
} as const
export const KEYBOARD_STEP = 16

export const PANE_LABELS: Record<PaneKey, string> = {
  navigator: 'ナビゲーター',
  toolbox: '分析ツール箱',
  inspector: 'プロパティ',
  messages: 'メッセージ',
}

function clamp(key: keyof typeof LIMITS, value: number) {
  const [min, max] = LIMITS[key]
  return Math.round(Math.min(max, Math.max(min, value)))
}

function readStoredLayout(storageKey: string, defaults: IdeLayoutState): IdeLayoutState {
  try {
    const raw = window.localStorage.getItem(`ide-layout:${storageKey}`)
    if (!raw) return defaults
    const parsed = JSON.parse(raw) as Partial<IdeLayoutState>
    return {
      navigatorWidth: clamp('navigatorWidth', Number(parsed.navigatorWidth ?? defaults.navigatorWidth)),
      toolboxWidth: clamp('toolboxWidth', Number(parsed.toolboxWidth ?? defaults.toolboxWidth)),
      inspectorWidth: clamp('inspectorWidth', Number(parsed.inspectorWidth ?? defaults.inspectorWidth)),
      messagesHeight: clamp('messagesHeight', Number(parsed.messagesHeight ?? defaults.messagesHeight)),
      visible: {
        navigator: parsed.visible?.navigator !== false,
        toolbox: parsed.visible?.toolbox !== false,
        inspector: parsed.visible?.inspector !== false,
        messages: parsed.visible?.messages !== false,
      },
    }
  } catch {
    return defaults
  }
}

export function useIdeLayout(
  storageKey: string,
  overrides: Partial<Omit<IdeLayoutState, 'visible'>> = {},
  /* 画面ごとのペイン名。ナビゲーターを「プリセット」と呼ぶ画面など。 */
  labelOverrides: Partial<Record<PaneKey, string>> = {},
) {
  const [labels] = useState<Record<PaneKey, string>>(() => ({ ...PANE_LABELS, ...labelOverrides }))
  /* 初期値は最初の描画で一度だけ決める。以後 overrides が変わっても追わない。 */
  const [defaults] = useState<IdeLayoutState>(() => ({ ...DEFAULT_LAYOUT, ...overrides }))
  const [layout, setLayout] = useState<IdeLayoutState>(() => readStoredLayout(storageKey, defaults))

  useEffect(() => {
    try {
      window.localStorage.setItem(`ide-layout:${storageKey}`, JSON.stringify(layout))
    } catch {
      /* 保存できない環境では、この表示のあいだだけ覚えておく。 */
    }
  }, [storageKey, layout])

  const togglePane = useCallback((pane: PaneKey) => {
    setLayout((current) => ({ ...current, visible: { ...current.visible, [pane]: !current.visible[pane] } }))
  }, [])
  const setPaneVisible = useCallback((pane: PaneKey, visible: boolean) => {
    setLayout((current) => ({ ...current, visible: { ...current.visible, [pane]: visible } }))
  }, [])
  const resize = useCallback((key: keyof typeof LIMITS, value: number) => {
    setLayout((current) => ({ ...current, [key]: clamp(key, value) }))
  }, [])
  const resetSize = useCallback((key: keyof typeof LIMITS) => {
    setLayout((current) => ({ ...current, [key]: defaults[key] }))
  }, [defaults])
  const reset = useCallback(() => setLayout(defaults), [defaults])

  return { layout, labels, togglePane, setPaneVisible, resize, resetSize, reset }
}

export type IdeLayoutControls = ReturnType<typeof useIdeLayout>
