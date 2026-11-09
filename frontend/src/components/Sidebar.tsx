import { Activity, BarChart3, ClipboardList, Clock3, GitBranch, LayoutDashboard, Play } from 'lucide-react'

export type Page =
  | 'overview'
  | 'run'
  | 'benchmark'
  | 'live'
  | 'history'
  | 'metrics'
  | 'repositories'

export const navItems: { id: Page; label: string; icon: any }[] = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'run', label: 'Run evaluation', icon: Play },
  { id: 'benchmark', label: 'Benchmark suite', icon: ClipboardList },
  { id: 'live', label: 'Live executions', icon: Activity },
  { id: 'history', label: 'Run history', icon: Clock3 },
  { id: 'metrics', label: 'Metrics', icon: BarChart3 },
  { id: 'repositories', label: 'Repositories', icon: GitBranch },
]

export default function TopNav({
  page,
  onNavigate,
  hasActiveRun,
}: {
  page: Page
  onNavigate: (p: Page) => void
  hasActiveRun?: boolean
}) {
  return (
    <nav className="tp-nav" aria-label="Main Navigation">
      {navItems.map((item) => {
        const Icon = item.icon
        const isActive = page === item.id
        return (
          <button
            key={item.id}
            className={`tp-tab ${isActive ? 'active' : ''}`}
            onClick={() => onNavigate(item.id)}
          >
            <Icon size={14} />
            <span>{item.label}</span>
            {item.id === 'live' && hasActiveRun && (
              <span className="online-dot" style={{ width: 6, height: 6 }} />
            )}
          </button>
        )
      })}
    </nav>
  )
}

const wheelAngles: Record<string, number> = {
  overview: 90,
  run: 60,
  benchmark: 30,
  live: 0,
  history: -30,
  metrics: -60,
  repositories: -90,
}

const wheelItems: { id: Page; label: string; angle: number }[] = [
  { id: 'overview', label: 'Overview', angle: -90 },
  { id: 'run', label: 'Run evaluation', angle: -60 },
  { id: 'benchmark', label: 'Benchmark suite', angle: -30 },
  { id: 'live', label: 'Live executions', angle: 0 },
  { id: 'history', label: 'Run history', angle: 30 },
  { id: 'metrics', label: 'Metrics', angle: 60 },
  { id: 'repositories', label: 'Repositories', angle: 90 },
]

export function WheelNav({
  page,
  onNavigate,
}: {
  page: Page
  onNavigate: (p: Page) => void
}) {
  const currentRotation = wheelAngles[page] ?? 90

  return (
    <nav className="wh" aria-label="Rotary Navigation">
      <div className="pt" />
      <div
        className="dl"
        style={{ transform: `rotate(${currentRotation}deg)` }}
      >
        <div className="rg" />
        <div className="tk" />
        {wheelItems.map((item) => {
          const isActive = page === item.id
          return (
            <div
              key={item.id}
              className="it"
              style={{ transform: `rotate(${item.angle}deg)` }}
            >
              <button
                type="button"
                className={isActive ? 'active' : ''}
                onClick={() => onNavigate(item.id)}
              >
                {item.label}
              </button>
            </div>
          )
        })}
      </div>
      <div className="hb">TURN THE WHEEL</div>
    </nav>
  )
}
