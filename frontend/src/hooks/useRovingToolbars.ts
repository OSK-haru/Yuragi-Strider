import { useEffect, useRef } from 'react'

/*
 * ARIA の toolbar は、Tab では「1つの部品」として止まり、中の移動は矢印キーで行う。
 * これが無いと、ゲートのボタン30個がそれぞれ Tab の止まり場所になり、ターミナルへ着くのに
 * 何十回も Tab を押すことになる。
 *
 * root の下の [role=toolbar] ごとに、押せるボタンのうち1つだけを tabindex=0 にする
 * （最後にフォーカスしたもの、無ければ先頭）。← → ↑ ↓ で隣へ、Home/End で端へ動く。
 * フォーカスや無効状態は DOM から読むので、React 側の状態は持たない。
 */

const TOOLBAR = '[role=toolbar]'
const ENABLED = 'button:not(:disabled)'

function enabledButtons(toolbar: Element): HTMLButtonElement[] {
  return Array.from(toolbar.querySelectorAll<HTMLButtonElement>(ENABLED))
}

function normalize(root: HTMLElement) {
  for (const toolbar of Array.from(root.querySelectorAll(TOOLBAR))) {
    const buttons = enabledButtons(toolbar)
    const current = buttons.find((button) => button.dataset.rovingCurrent === 'true') ?? buttons[0]
    for (const button of Array.from(toolbar.querySelectorAll('button'))) {
      button.tabIndex = button === current ? 0 : -1
    }
  }
}

export function useRovingToolbars<T extends HTMLElement>() {
  const rootRef = useRef<T>(null)

  // 描画のたびに整える（無効になったボタンや、追加されたボタンに追従する）。
  useEffect(() => {
    if (rootRef.current) normalize(rootRef.current)
  })

  useEffect(() => {
    const root = rootRef.current
    if (!root) return undefined

    const remember = (event: FocusEvent) => {
      const target = event.target as HTMLElement
      const toolbar = target.closest(TOOLBAR)
      if (!toolbar || target.tagName !== 'BUTTON') return
      for (const button of Array.from(toolbar.querySelectorAll('button'))) delete button.dataset.rovingCurrent
      target.dataset.rovingCurrent = 'true'
      normalize(root)
    }

    const move = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      const toolbar = target.closest(TOOLBAR)
      if (!toolbar || target.tagName !== 'BUTTON' || event.altKey || event.ctrlKey || event.metaKey) return
      const buttons = enabledButtons(toolbar)
      const index = buttons.indexOf(target as HTMLButtonElement)
      if (index < 0) return
      let next = -1
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % buttons.length
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + buttons.length) % buttons.length
      else if (event.key === 'Home') next = 0
      else if (event.key === 'End') next = buttons.length - 1
      if (next < 0) return
      event.preventDefault()
      buttons[next].focus()
    }

    root.addEventListener('focusin', remember)
    root.addEventListener('keydown', move)
    return () => {
      root.removeEventListener('focusin', remember)
      root.removeEventListener('keydown', move)
    }
  }, [])

  return rootRef
}
