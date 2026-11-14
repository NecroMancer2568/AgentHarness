import { ReactNode } from 'react'
import { ArrowUpRight, CheckCircle2, AlertCircle, AlertTriangle, Clock3, Loader2, TerminalSquare, XCircle } from 'lucide-react'

export function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="hd">
      <div>
        <div className="ey">{eyebrow}</div>
        <h1 className="h1">{title}</h1>
        {description && <p className="pp">{description}</p>}
      </div>
      {action && <div>{action}</div>}
    </div>
  )
}

export function StatCard({
  label,
  value,
  detail,
}: {
  label: string
  value: string | number
  detail?: string
  icon?: any
  tone?: string
}) {
  return (
    <div className="c">
      <div className="lb">{label}</div>
      <div className="nm">{value}</div>
      {detail && <div className="sb">{detail}</div>}
    </div>
  )
}

export function StatusPill({ status }: { status: string }) {
  const s = String(status || '').toLowerCase()
  const isSuccess =
    s === 'succeeded' || s === 'pass' || s === 'approved' || s === 'success' || s === 'success_with_warnings'
  const Icon = isSuccess
    ? CheckCircle2
    : s === 'partial_pass' || s === 'partial'
    ? AlertTriangle
    : s === 'failed' || s === 'fail' || s === 'rejected'
    ? XCircle
    : s === 'running'
    ? Loader2
    : s === 'timed_out' || s === 'queued'
    ? Clock3
    : AlertCircle

  const label =
    s === 'success_with_warnings' || s === 'succeeded' || s === 'success'
      ? 'SUCCESS'
      : (status || '').replace(/_/g, ' ').toUpperCase()

  const pillClass = isSuccess ? 'pl succeeded' : `pl ${s}`

  return (
    <span className={pillClass}>
      <Icon size={13} className={s === 'running' ? 'spin' : ''} />
      {label}
    </span>
  )
}

export function Button({
  children,
  onClick,
  kind = 'primary',
  disabled = false,
}: {
  children: ReactNode
  onClick?: () => void
  kind?: 'primary' | 'secondary' | 'ghost' | 'danger'
  disabled?: boolean
}) {
  const cls = `bt ${kind === 'secondary' ? 'secondary' : kind === 'danger' ? 'danger' : ''}`
  return (
    <button disabled={disabled} className={cls} onClick={onClick}>
      {children}
      {kind === 'primary' && <ArrowUpRight size={16} />}
    </button>
  )
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string
  body: string
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <TerminalSquare size={24} />
      </div>
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  )
}

export function formatDuration(ms?: number | null) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

export function formatNumber(n?: number | null) {
  return n == null ? '—' : n.toLocaleString()
}
