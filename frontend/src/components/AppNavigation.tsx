import { useEffect, useState } from 'react'
import './AppNavigation.css'
import { SettingsMenu } from './SettingsMenu'
import { documentationLinks } from '../utils/documentationLinks'

export type NavigationRoute =
  | 'home'
  | 'gate-aware-ide'
  | 'pulse-ide'
  | 'help'
  | 'attack-simulator'

type AppNavigationProps = {
  currentRoute: NavigationRoute
  onNavigate: (route: NavigationRoute) => void
}

type NavigationItem = { route: NavigationRoute; label: string; detail: string }

const homeItem: NavigationItem = { route: 'home', label: 'ホーム', detail: 'Yuragi-Strider' }
const attackItem: NavigationItem = { route: 'attack-simulator', label: '攻撃シミュレーター', detail: '環境設定 / live操作' }

const gateAwareItems: NavigationItem[] = [
  { route: 'gate-aware-ide', label: 'IDEワークスペース', detail: '回路・実行・状態エクスプローラー' },
  { route: 'help', label: 'ヘルプ / Q&A', detail: 'Guide' },
]

const pulseItems: NavigationItem[] = [
  { route: 'pulse-ide', label: 'Pulse IDE', detail: 'Pulse 回路・実行・状態エクスプローラー' },
]

/* レール左側に出す、現在地の機械的な呼称。 */
const routeDesignations: Record<NavigationRoute, string> = {
  home: 'IDX / PRIMARY TERMINAL',
  'gate-aware-ide': 'IDE / GATE-AWARE',
  'pulse-ide': 'IDE / PULSE',
  help: 'DOC / GUIDE',
  'attack-simulator': 'ATK / SHARED SERVICE',
}

type NavigationDomain = 'home' | 'gate-aware' | 'pulse' | 'attack'

function navigationDomainForRoute(route: NavigationRoute): NavigationDomain {
  if (route === 'attack-simulator') return 'attack'
  if (route === 'home') {
    return 'home'
  }
  return route === 'pulse-ide'
    ? 'pulse'
    : 'gate-aware'
}

/* メニューは3つのフォルダに分ける。現在地のフォルダだけ開いた状態で始める。 */
type NavigationGroup = {
  domain: Exclude<NavigationDomain, 'home'>
  label: string
  detail: string
  items: NavigationItem[]
}

const navigationGroups: NavigationGroup[] = [
  { domain: 'gate-aware', label: '通常モード', detail: 'Gate-aware', items: gateAwareItems },
  { domain: 'attack', label: '攻撃シミュレーター', detail: 'Attack', items: [attackItem] },
  { domain: 'pulse', label: 'Pulseモード', detail: 'Pulse', items: pulseItems },
]

export function AppNavigation({ currentRoute, onNavigate }: AppNavigationProps) {
  const [isOpen, setIsOpen] = useState(false)
  const domain = navigationDomainForRoute(currentRoute)
  const [openGroups, setOpenGroups] = useState<NavigationDomain[]>([domain])

  // Opening the group of the current route is derived while rendering, not in an effect.
  const [shownDomain, setShownDomain] = useState(domain)
  if (shownDomain !== domain) {
    setShownDomain(domain)
    setOpenGroups((open) => (open.includes(domain) ? open : [...open, domain]))
  }

  function toggleGroup(group: NavigationDomain) {
    setOpenGroups((open) =>
      open.includes(group) ? open.filter((entry) => entry !== group) : [...open, group],
    )
  }

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setIsOpen(false)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [])

  function navigate(route: NavigationRoute) {
    setIsOpen(false)
    onNavigate(route)
  }

  function renderItem(item: NavigationItem) {
    return (
      <button
        className={`app-navigation__item${currentRoute === item.route ? ' app-navigation__item--current' : ''}`}
        type="button"
        key={item.route}
        aria-current={currentRoute === item.route ? 'page' : undefined}
        onClick={() => navigate(item.route)}
      >
        <span>{item.label}</span>
        <small>{item.detail}</small>
      </button>
    )
  }

  return (
    <div className="app-navigation">
      {isOpen ? (
        <button
          className="app-navigation__scrim"
          type="button"
          aria-label="メニューを閉じる"
          onClick={() => setIsOpen(false)}
        />
      ) : null}

      {/* 全ページ共通の計器レール。現在地と機体情報をここに集約する。 */}
      <div className="app-navigation__rail">
        <span className="app-navigation__mark">
          YURAGI&ndash;STRIDER
        </span>
        <span className="app-navigation__route">{routeDesignations[currentRoute]}</span>
        <a
          className="app-navigation__docs"
          href={documentationLinks.home}
          target="_blank"
          rel="noreferrer"
          aria-label="ドキュメントサイトを新しいタブで開く"
        >
          DOCS <span aria-hidden="true">↗</span>
        </a>

        <button
          className={`app-navigation__toggle${isOpen ? ' app-navigation__toggle--open' : ''}`}
          type="button"
          aria-label={isOpen ? 'ナビゲーションを閉じる' : 'ナビゲーションを開く'}
          aria-expanded={isOpen}
          aria-controls="global-navigation-menu"
          onClick={() => setIsOpen((open) => !open)}
        >
          <span className="app-navigation__toggle-label" aria-hidden="true">
            {isOpen ? 'CLOSE' : 'MENU'}
          </span>
          <span className="app-navigation__toggle-icon" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </button>
      </div>
      <nav
        id="global-navigation-menu"
        className={`app-navigation__menu${isOpen ? ' app-navigation__menu--open' : ''}`}
        aria-label="ページナビゲーション"
        /*
         * 閉じている間は opacity 0 で見えないだけなので、aria-hidden だけだと
         * Tab で見えない項目へフォーカスが入る。inert で操作・読み上げ両方から外す。
         */
        inert={!isOpen}
      >
        <div className="app-navigation__menu-header">
          <span>YURAGI-STRIDER</span>
          <small>
            {domain === 'home'
              ? 'モードを選択'
              : domain === 'pulse'
                ? 'Pulseワークスペース'
                : domain === 'attack'
                  ? '攻撃ワークスペース'
                : 'Gate-awareワークスペース'}
          </small>
        </div>
        {renderItem(homeItem)}
        {navigationGroups.map((group) => {
          const expanded = openGroups.includes(group.domain)
          const panelId = `global-navigation-group-${group.domain}`
          return (
            <div className="app-navigation__group" key={group.domain}>
              <button
                className={`app-navigation__folder${expanded ? ' app-navigation__folder--open' : ''}`}
                type="button"
                aria-expanded={expanded}
                aria-controls={panelId}
                onClick={() => toggleGroup(group.domain)}
              >
                <span>{group.label}</span>
                <small>{group.detail}</small>
              </button>
              <div className="app-navigation__group-items" id={panelId} hidden={!expanded}>
                {group.items.map(renderItem)}
              </div>
            </div>
          )
        })}
        <a
          className="app-navigation__item app-navigation__item--external"
          href={documentationLinks.home}
          target="_blank"
          rel="noreferrer"
        >
          <span>ドキュメントサイト</span>
          <small>Manual / Physics ↗</small>
        </a>
        <SettingsMenu />
      </nav>
    </div>
  )
}
