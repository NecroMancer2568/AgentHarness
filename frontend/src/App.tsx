import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  CircleDot,
  Clock3,
  FileArchive,
  FileCode2,
  FolderPlus,
  GitBranch,
  HardDrive,
  Layers3,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Timer,
  Trash2,
  UploadCloud,
  Zap,
} from 'lucide-react'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import TopNav, { Page, WheelNav } from './components/Sidebar'
import { Button, EmptyState, formatDuration, formatNumber, PageHeader, StatCard, StatusPill } from './components/Primitives'
import { api, BenchmarkRun, BenchmarkTask, EventItem, Repository, Run } from './api'

function App() {
  const [page, setPage] = useState<Page>('overview')
  const [runs, setRuns] = useState<Run[]>([])
  const [metrics, setMetrics] = useState<any>({})
  const [repos, setRepos] = useState<Repository[]>([])
  const [settings, setSettings] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedRun, setSelectedRun] = useState<Run | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [selectedRepoForRun, setSelectedRepoForRun] = useState('')
  const [showIntro, setShowIntro] = useState(true)

  useEffect(() => {
    const timer = setTimeout(() => setShowIntro(false), 3400)
    return () => clearTimeout(timer)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [r, m, repo, s] = await Promise.all([
        api.runs(100),
        api.metrics(),
        api.repositories(),
        api.settings(),
      ])
      setRuns(r.runs)
      setMetrics(m)
      setRepos(repo.repositories)
      setSettings(s)
      setError('')
    } catch (e: any) {
      setError(e.message || 'Unable to connect to the API. Start the FastAPI backend.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  const openRun = useCallback(async (id: string) => {
    setSelectedRunId(id)
    setPage('live')
    try {
      const [r, e] = await Promise.all([api.run(id), api.events(id)])
      setSelectedRun(r)
      setEvents(e.events)
    } catch (e: any) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ id: string }>
      if (custom.detail?.id) openRun(custom.detail.id)
    }
    window.addEventListener('agentharness:open-run', handler)
    return () => window.removeEventListener('agentharness:open-run', handler)
  }, [openRun])

  useEffect(() => {
    if (!selectedRunId || page !== 'live') return
    if (selectedRun && ['succeeded', 'success_with_warnings', 'failed', 'cancelled', 'timed_out'].includes(selectedRun.status)) return
    let alive = true
    const poll = async () => {
      try {
        const [r, e] = await Promise.all([
          api.run(selectedRunId),
          api.events(selectedRunId, events.at(-1)?.id || 0),
        ])
        if (!alive) return
        setSelectedRun(r)
        if (e.events.length) {
          setEvents((prev) => [
            ...prev,
            ...e.events.filter((x) => !prev.some((p) => p.id === x.id)),
          ])
        }
        if (['succeeded', 'success_with_warnings', 'failed', 'cancelled', 'timed_out'].includes(r.status)) {
          clearInterval(timer)
        }
      } catch {}
    }
    poll()
    const timer = setInterval(poll, 1500)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [selectedRunId, page, events, selectedRun?.status])

  const activeRuns = runs.filter((r) => r.status === 'running' || r.status === 'queued')
  const navigate = (next: Page) => {
    setPage(next)
    if (next !== 'live') {
      setSelectedRunId('')
      setSelectedRun(null)
      setEvents([])
    }
  }

  return (
    <div className="rt">
      {/* 3D Intro Animation */}
      {showIntro && (
        <div className="pre" aria-hidden="true">
          <div className="pw">
            <span style={{ '--i': 0 } as any}>A</span>
            <span style={{ '--i': 1 } as any}>g</span>
            <span style={{ '--i': 2 } as any}>e</span>
            <span style={{ '--i': 3 } as any}>n</span>
            <span style={{ '--i': 4 } as any}>t</span>
            <span className="hl" style={{ '--i': 5 } as any}>H</span>
            <span className="hl" style={{ '--i': 6 } as any}>a</span>
            <span className="hl" style={{ '--i': 7 } as any}>r</span>
            <span className="hl" style={{ '--i': 8 } as any}>n</span>
            <span className="hl" style={{ '--i': 9 } as any}>e</span>
            <span className="hl" style={{ '--i': 10 } as any}>s</span>
            <span className="hl" style={{ '--i': 11 } as any}>s</span>
          </div>
          <div className="ps2">LOCAL WORKBENCH</div>
          <div className="pb">
            <i />
          </div>
        </div>
      )}

      {/* Glowing background orbs */}
      <div className="b b1" />
      <div className="b b2" />

      {/* Top bar */}
      <header className="tp">
        <div className="br">
          <div className="lg" />
          <span>AgentHarness</span>
          <span
            className="mono"
            style={{
              fontSize: 11,
              fontWeight: 400,
              letterSpacing: '.14em',
              color: 'rgba(255,255,255,.6)',
            }}
          >
            LOCAL WORKBENCH
          </span>
        </div>

        <TopNav
          page={page}
          onNavigate={navigate}
          hasActiveRun={activeRuns.length > 0}
        />

        <div className="tp-right">
          <div className="ch">
            <span className="online-dot" />
            <span>{settings.model || 'DeepSeek V4 Pro'}</span>
          </div>
          <button
            className="icon-btn"
            title="Refresh data"
            onClick={refresh}
          >
            <RefreshCw size={15} />
          </button>
        </div>
      </header>

      {/* Main Content */}
      <main className="mn">
        {error && (
          <div className="error-banner">
            <CircleDot size={16} />
            <span>{error}</span>
            <button onClick={refresh}>Retry</button>
          </div>
        )}

        {page === 'overview' && (
          <Overview
            runs={runs}
            metrics={metrics}
            repos={repos}
            loading={loading}
            onNavigate={navigate}
            onOpenRun={openRun}
          />
        )}

        {page === 'run' && (
          <RunEvaluation
            repos={repos}
            initialRepo={selectedRepoForRun}
            onCreated={async (id) => {
              await refresh()
              await openRun(id)
            }}
            onNavigate={navigate}
          />
        )}

        {page === 'benchmark' && <BenchmarkPage />}

        {page === 'live' && (
          <LiveExecution
            run={selectedRun}
            events={events}
            onCancel={async () => {
              if (selectedRunId) {
                await api.cancel(selectedRunId)
                await refresh()
              }
            }}
            onRefresh={refresh}
          />
        )}

        {page === 'history' && (
          <RunHistory
            runs={runs}
            onOpen={openRun}
            onNavigate={navigate}
          />
        )}

        {page === 'metrics' && (
          <MetricsPage
            metrics={metrics}
            runs={runs}
            onNavigate={navigate}
          />
        )}

        {page === 'repositories' && (
          <RepositoriesPage
            repos={repos}
            root={settings.repos_root}
            onRegistered={refresh}
            onNavigate={navigate}
            onSelectRepoForRun={(r) => setSelectedRepoForRun(r)}
          />
        )}
      </main>

      {/* Rotary Wheel Navigation docked at the bottom */}
      <WheelNav page={page} onNavigate={navigate} />
    </div>
  )
}

function Overview({
  runs,
  metrics,
  repos: _repos,
  loading: _loading,
  onNavigate,
  onOpenRun,
}: {
  runs: Run[]
  metrics: any
  repos: Repository[]
  loading: boolean
  onNavigate: (p: Page) => void
  onOpenRun: (id: string) => void
}) {
  const recent = runs.slice(0, 5)
  const chart = useMemo(
    () =>
      runs
        .slice(0, 10)
        .reverse()
        .map((r, i) => ({
          name: `#${i + 1}`,
          duration: r.duration_ms ? Math.round(r.duration_ms / 1000) : 0,
          tokens: r.total_tokens || 0,
        })),
    [runs]
  )

  return (
    <>
      <PageHeader
        eyebrow="Workspace"
        title="Overview"
        description="Run real software tasks, inspect agent traces, and track verified outcomes."
        action={
          <Button onClick={() => onNavigate('run')}>
            <Play size={15} /> New evaluation
          </Button>
        }
      />

      <div className="gr c4">
        <StatCard
          label="Verified success rate"
          value={metrics.success_rate == null ? '0%' : `${metrics.success_rate}%`}
          detail={`${formatNumber(metrics.successful_runs || 0)} successful runs`}
        />
        <StatCard
          label="Completed runs"
          value={formatNumber(metrics.completed_runs || 0)}
          detail={`${formatNumber(runs.filter((r) => r.status === 'running').length)} currently running`}
        />
        <StatCard
          label="Total LLM tokens"
          value={formatNumber(metrics.total_tokens || 0)}
          detail={`${formatNumber(metrics.llm_calls || 0)} recorded model calls`}
        />
        <StatCard
          label="Average duration"
          value={formatDuration(metrics.average_duration_ms)}
          detail={`p95 ${formatDuration(metrics.p95_duration_ms)}`}
        />
      </div>

      <div className="gr c2">
        <div className="c">
          <h2>Execution duration</h2>
          <div className="sb">Recent recorded runs · seconds</div>
          {chart.length ? (
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chart} margin={{ top: 12, right: 12, left: -20, bottom: 0 }}>
                  <defs>
                    <linearGradient id="durationGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#ffffff" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="#ffffff" stopOpacity={0.01} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="4 6" stroke="rgba(255,255,255,.12)" vertical={false} />
                  <XAxis dataKey="name" tick={{ fill: 'rgba(255,255,255,.6)', fontSize: 12, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fill: 'rgba(255,255,255,.6)', fontSize: 12, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
                  <Tooltip
                    contentStyle={{
                      background: '#0d0d0d',
                      border: '1px solid rgba(255,255,255,0.2)',
                      borderRadius: 14,
                      color: '#ffffff',
                      fontFamily: 'Space Grotesk',
                    }}
                    formatter={(v: any) => [`${v}s`, 'Duration']}
                  />
                  <Area type="monotone" dataKey="duration" stroke="#ffffff" strokeWidth={2.5} fill="url(#durationGrad)" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <EmptyState
              title="No execution data yet"
              body="Run a task to populate the duration chart."
              action={<Button onClick={() => onNavigate('run')}>Run first task</Button>}
            />
          )}
        </div>

        <div className="c">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h2>Recent executions</h2>
            <button className="bt secondary sm" onClick={() => onNavigate('history')}>
              All runs <ArrowRight size={13} />
            </button>
          </div>
          {recent.length ? (
            <div>
              {recent.map((r) => (
                <div className="rw clickable-row" key={r.id} onClick={() => onOpenRun(r.id)}>
                  <div>
                    <strong style={{ fontSize: 15 }}>{r.repository}</strong>
                    <div className="sb" style={{ marginTop: 4 }}>
                      {r.task.slice(0, 48)}... · {new Date(r.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 14, alignItems: 'center' }}>
                    <StatusPill status={r.status} />
                    <span className="mono" style={{ fontSize: 13, color: 'rgba(255,255,255,0.7)' }}>
                      {formatDuration(r.duration_ms)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              title="Nothing run yet"
              body="Your recent task executions will appear here."
              action={<Button onClick={() => onNavigate('run')}>Start an evaluation</Button>}
            />
          )}
        </div>
      </div>
    </>
  )
}

function BenchmarkPage() {
  const [tasks, setTasks] = useState<BenchmarkTask[]>([])
  const [datasets, setDatasets] = useState<{ name: string; task_count: number | null; is_default: boolean; error?: string }[]>([])
  const [dataset, setDataset] = useState('tasks.jsonl')
  const [history, setHistory] = useState<BenchmarkRun[]>([])
  const [selected, setSelected] = useState<BenchmarkRun | null>(null)
  const [repetitions, setRepetitions] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const [d, h] = await Promise.all([api.benchmarkDatasets(), api.benchmarks()])
      setDatasets(d.datasets)
      setHistory(h.benchmarks)
      const current = d.datasets.find((x) => x.name === dataset)
      if (!current && d.datasets.length) setDataset(d.datasets[0].name)
      setError('')
    } catch (e: any) {
      setError(e.message || 'Unable to load benchmark suite. Start the backend and check the manifest.')
    }
  }, [dataset])

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    let alive = true
    api.benchmarkTasks(dataset)
      .then((value) => {
        if (alive) setTasks(value.tasks)
      })
      .catch((e: any) => {
        if (alive) setError(e.message)
      })
    return () => {
      alive = false
    }
  }, [dataset])

  useEffect(() => {
    if (!selected || !['queued', 'running'].includes(selected.status)) return
    let alive = true
    const poll = async () => {
      try {
        const latest = await api.benchmark(selected.id)
        if (!alive) return
        setSelected(latest)
        const list = await api.benchmarks()
        if (alive) setHistory(list.benchmarks)
      } catch (e: any) {
        if (alive) setError(e.message)
      }
    }
    poll()
    const timer = setInterval(poll, 2000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [selected?.id, selected?.status])

  const start = async () => {
    setBusy(true)
    setError('')
    try {
      const created = await api.startBenchmark(repetitions, dataset)
      const run = await api.benchmark(created.id)
      setSelected(run)
      await refresh()
    } catch (e: any) {
      setError(e.message || 'Could not start benchmark. Confirm all benchmark repositories are available.')
    } finally {
      setBusy(false)
    }
  }

  const open = async (id: string) => {
    try {
      setSelected(await api.benchmark(id))
      setError('')
    } catch (e: any) {
      setError(e.message)
    }
  }

  const cancel = async () => {
    if (!selected) return
    try {
      await api.cancelBenchmark(selected.id)
      setSelected(await api.benchmark(selected.id))
    } catch (e: any) {
      setError(e.message)
    }
  }

  const summary = selected?.summary || {}
  const fmt = (n: any) => (n == null ? '—' : Number(n).toLocaleString())

  return (
    <>
      <PageHeader
        eyebrow="Repeatable engineering checks"
        title="Benchmark suite"
        description="Run the same fixed task set against the configured harness. Scores come from actual repository runs, test results, and reviewer gates."
        action={
          <Button
            onClick={start}
            disabled={busy || tasks.length === 0 || (!!selected && ['queued', 'running'].includes(selected.status))}
          >
            ▶ {busy ? 'Starting…' : 'Run benchmark'}
          </Button>
        }
      />

      {error && (
        <div className="error-banner">
          <CircleDot size={16} />
          <span>{error}</span>
          <button onClick={refresh}>Retry</button>
        </div>
      )}

      <div className="c" style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 16 }}>
          <div>
            <h2>Default task set</h2>
            <div className="sb">{tasks.length} tasks · each sample runs in a disposable clone</div>
          </div>
          <div style={{ display: 'flex', gap: 14, alignItems: 'center' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14 }}>
              Dataset
              <select className="in" style={{ width: 'auto', padding: '8px 14px' }} value={dataset} onChange={(e) => setDataset(e.target.value)}>
                {datasets.map((d) => (
                  <option key={d.name} value={d.name}>
                    {d.name} ({d.task_count ?? 'invalid'})
                  </option>
                ))}
              </select>
            </label>

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14 }}>
              Repetitions
              <select className="in" style={{ width: 'auto', padding: '8px 14px' }} value={repetitions} onChange={(e) => setRepetitions(Number(e.target.value))}>
                <option value={1}>1×</option>
                <option value={2}>2×</option>
                <option value={3}>3×</option>
                <option value={5}>5×</option>
              </select>
            </label>

            <button className="bt secondary sm" onClick={refresh}>
              <RefreshCw size={14} /> Refresh
            </button>
          </div>
        </div>
      </div>

      <div className="gr e2">
        {tasks.map((t) => (
          <div className="c" key={t.id} style={{ padding: 20 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <span className="pl">{t.category}</span>
              <span className="mono" style={{ fontSize: 12, color: 'rgba(255,255,255,0.6)' }}>{t.id}</span>
            </div>
            <p style={{ fontWeight: 700, margin: '12px 0 14px', fontSize: 15, lineHeight: 1.4 }}>
              {t.expected_behavior || t.input}
            </p>
            <div className="in mono" style={{ fontSize: 13, background: 'rgba(0,0,0,0.6)', padding: '10px 14px' }}>
              {t.test_command}
            </div>
          </div>
        ))}
      </div>

      {selected && (
        <div style={{ marginTop: 32 }}>
          <div className="c" style={{ marginBottom: 20 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <h2>Benchmark Run {selected.id}</h2>
                  <StatusPill status={selected.status === 'completed' ? (summary.failed_samples === 0 ? 'succeeded' : 'failed') : selected.status} />
                </div>
                <div className="sb" style={{ marginTop: 4 }}>
                  {selected.completed_samples}/{selected.planned_samples} samples completed · {selected.model}
                </div>
              </div>
              {['queued', 'running'].includes(selected.status) && (
                <button className="bt danger sm" onClick={cancel}>
                  Cancel benchmark
                </button>
              )}
            </div>
          </div>

          <div className="gr c4" style={{ marginBottom: 20 }}>
            <StatCard label="Success rate" value={summary.success_rate_percent == null ? '—' : `${summary.success_rate_percent}%`} detail={`${fmt(summary.successful_samples)} / ${fmt(summary.attempted_samples)} samples`} />
            <StatCard label="Avg duration" value={formatDuration(summary.average_duration_ms)} detail={`p95 ${formatDuration(summary.p95_duration_ms)}`} />
            <StatCard label="Total tokens" value={fmt(summary.total_tokens)} detail={`${fmt(summary.total_llm_calls)} model calls`} />
            <StatCard label="Test pass count" value={fmt(summary.test_pass_count)} detail={`${fmt(summary.review_approval_count)} reviews approved`} />
          </div>

          <div className="c">
            <h2>Sample-level results</h2>
            {selected.results?.length ? (
              <table style={{ marginTop: 14 }}>
                <thead>
                  <tr>
                    <th>Task</th>
                    <th>Rep</th>
                    <th>Status</th>
                    <th>Tests</th>
                    <th>Review</th>
                    <th>Duration</th>
                    <th>Tokens</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {selected.results.map((r, i) => (
                    <tr key={`${r.task_id}-${r.repetition}-${i}`}>
                      <td>
                        <strong>{r.task_id}</strong>
                        <div className="sb">{r.repository}</div>
                      </td>
                      <td>{r.repetition}</td>
                      <td>
                        <StatusPill status={r.success ? 'succeeded' : r.status} />
                      </td>
                      <td>{r.test_passed ? 'Passed' : 'Not passed'}</td>
                      <td>{r.review_approved ? 'Approved' : 'Not approved'}</td>
                      <td>{formatDuration(r.duration_ms)}</td>
                      <td>{fmt(r.total_tokens)}</td>
                      <td>
                        <button
                          className="bt secondary sm"
                          onClick={() => {
                            window.dispatchEvent(new CustomEvent('agentharness:open-run', { detail: { id: r.run_id } }))
                          }}
                        >
                          Inspect <ArrowRight size={12} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState title="Waiting for benchmark samples" body="The sample table will populate as each run completes." />
            )}
          </div>
        </div>
      )}

      <div className="c" style={{ marginTop: 24 }}>
        <h2>Previous benchmark runs</h2>
        {history.length ? (
          <table style={{ marginTop: 14 }}>
            <thead>
              <tr>
                <th>Benchmark</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Success rate</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id}>
                  <td>
                    <strong>{h.id}</strong>
                    <div className="sb">{h.model}</div>
                  </td>
                  <td>
                    <StatusPill status={h.status} />
                  </td>
                  <td>
                    {h.completed_samples}/{h.planned_samples}
                  </td>
                  <td>{h.summary?.success_rate_percent == null ? '—' : `${h.summary.success_rate_percent}%`}</td>
                  <td>{new Date(h.created_at).toLocaleString()}</td>
                  <td>
                    <button className="bt secondary sm" onClick={() => open(h.id)}>
                      Open <ArrowRight size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState title="No benchmark history yet" body="Start a benchmark to create the first report." />
        )}
      </div>
    </>
  )
}

function RunEvaluation({
  repos,
  initialRepo,
  onCreated,
  onNavigate,
}: {
  repos: Repository[]
  initialRepo?: string
  onCreated: (id: string) => void
  onNavigate?: (p: Page) => void
}) {
  const [repository, setRepository] = useState(initialRepo || repos[0]?.name || '')
  const [task, setTask] = useState('Find and fix a reproducible bug. Add or update tests to verify the fix, then explain the changes.')
  const [testCommand, setTestCommand] = useState('auto')
  const [maxSteps, setMaxSteps] = useState(8)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (initialRepo) setRepository(initialRepo)
    else if (!repository && repos.length) setRepository(repos[0].name)
  }, [repos, repository, initialRepo])

  const submit = async () => {
    setError('')
    if (!repository) {
      setError('Register or clone a Git repository first.')
      return
    }
    if (task.trim().length < 10) {
      setError('Describe the task in at least 10 characters.')
      return
    }
    setSubmitting(true)
    try {
      const r = await api.createRun({
        repository,
        task: task.trim(),
        test_command: testCommand,
        max_agent_steps: maxSteps,
      })
      onCreated(r.id)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="New agent run"
        title="Run evaluation"
        description="Describe the change, set a budget, let six agents work in an isolated clone."
      />

      <div className="gr c2">
        <div className="c">
          <label className="fl" htmlFor="rp" style={{ marginTop: 0 }}>
            Repository
          </label>
          <select
            id="rp"
            className="in"
            value={repository}
            onChange={(e) => setRepository(e.target.value)}
          >
            {repos.length === 0 && <option value="">No repositories registered</option>}
            {repos.map((r) => (
              <option key={r.name} value={r.name}>
                {r.name} — {r.path}
              </option>
            ))}
          </select>
          {repos.length === 0 && (
            <div className="sb" style={{ marginTop: 8 }}>
              No repositories found.{' '}
              <button
                type="button"
                style={{ background: 'transparent', border: 0, color: '#fff', textDecoration: 'underline', cursor: 'pointer' }}
                onClick={() => onNavigate?.('repositories')}
              >
                Add or clone a repository
              </button>
            </div>
          )}

          <label className="fl" htmlFor="tk">
            Engineering task
          </label>
          <textarea
            id="tk"
            className="in"
            rows={4}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            placeholder="Describe the bug, feature, or refactor…"
          />

          <label className="fl" htmlFor="tc">
            Test command
          </label>
          <select
            id="tc"
            className="in"
            value={testCommand}
            onChange={(e) => setTestCommand(e.target.value)}
          >
            <option value="auto">Auto-detect repository test runner</option>
            <option value="pytest">pytest</option>
            <option value="python -m pytest">python -m pytest</option>
            <option value="npm test">npm test</option>
            <option value="npm run test">npm run test</option>
            <option value="cargo test">cargo test</option>
            <option value="go test ./...">go test ./...</option>
            <option value="mvn test">mvn test</option>
            <option value="gradle test">gradle test</option>
          </select>

          <label className="fl">
            Maximum tool steps per agent: <b>{maxSteps}</b>
          </label>
          <input
            className="range-input"
            type="range"
            min="2"
            max="16"
            value={maxSteps}
            onChange={(e) => setMaxSteps(Number(e.target.value))}
          />
          <div className="range-labels">
            <span>2 · Fast budget</span>
            <span>16 · Thorough investigation</span>
          </div>

          {error && <div className="inline-error">{error}</div>}

          <div style={{ marginTop: 26, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span className="sb">
              <ShieldCheck size={15} style={{ verticalAlign: 'middle', marginRight: 6 }} />
              Disposable clone
            </span>
            <Button onClick={submit} disabled={submitting || repos.length === 0}>
              {submitting ? 'Starting…' : 'Start agent run'} <ArrowRight size={15} />
            </Button>
          </div>
        </div>

        <div className="c">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
            <Layers3 size={20} />
            <h2>Agent workflow</h2>
          </div>
          {[
            { n: '01', title: 'Coordinator', desc: 'Plans task and routes specialist review' },
            { n: '02', title: 'Repository analyst', desc: 'Inspects code and gathers evidence' },
            { n: '03', title: 'Test scout', desc: 'Finds test framework and regressions' },
            { n: '04', title: 'Coding agent', desc: 'Implements changes in isolated clone' },
            { n: '05', title: 'Test & debug', desc: 'Runs actual tests and diagnoses failures' },
            { n: '06', title: 'Code reviewer', desc: 'Reviews diff and verification evidence' },
          ].map((a) => (
            <div className="workflow-item" key={a.n}>
              <span>{a.n}</span>
              <div>
                <strong>{a.title}</strong>
                <small>{a.desc}</small>
              </div>
            </div>
          ))}
          <div className="sb" style={{ marginTop: 18, borderTop: '1px solid rgba(255,255,255,0.1)', paddingTop: 14 }}>
            <Zap size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Read-only discovery agents run concurrently. Implementation, testing, and review follow dependency order.
          </div>
        </div>
      </div>
    </>
  )
}

function LiveExecution({
  run,
  events,
  onCancel,
  onRefresh,
}: {
  run: Run | null
  events: EventItem[]
  onCancel: () => void
  onRefresh: () => void
}) {
  const [tab, setTab] = useState<'timeline' | 'agents' | 'diff' | 'metrics'>('timeline')
  const [detail, setDetail] = useState<EventItem | null>(null)

  useEffect(() => {
    if (events.length && !detail) setDetail(events[events.length - 1])
  }, [events, detail])

  if (!run) {
    return (
      <>
        <PageHeader
          eyebrow="Live observability"
          title="Live execution inspector"
          description="Select a run from history or start a new evaluation to inspect real agent traces."
        />
        <EmptyState
          title="No run selected"
          body="Open an execution from Overview or Run history."
          action={<Button onClick={onRefresh}>Refresh runs</Button>}
        />
      </>
    )
  }

  const result = run.result || {}
  const isActive = run.status === 'running' || run.status === 'queued'
  const warnings = run.warnings || result.warnings || []
  const notification = run.notification || result.notification
  const verificationDetails = result.verification_details || {}
  const checksPassed: string[] = verificationDetails.checks_passed || []
  const checksSkipped: string[] = verificationDetails.checks_skipped || []

  return (
    <>
      <PageHeader
        eyebrow={`EXECUTION / #${run.id}`}
        title={run.repository}
        description={run.task}
        action={
          <div style={{ display: 'flex', gap: 12 }}>
            {isActive && (
              <Button kind="danger" onClick={onCancel}>
                Cancel run
              </Button>
            )}
            <Button kind="secondary" onClick={onRefresh}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </div>
        }
      />

      <div
        className="c"
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 16,
          padding: '16px 24px',
          marginBottom: 20,
        }}
      >
        <StatusPill status={run.status} />
        <span className="mono">
          <Clock3 size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          {formatDuration(run.duration_ms)}
        </span>
        <span className="mono">
          <Zap size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          {formatNumber(run.total_tokens)} tokens
        </span>
        <span className="mono">
          <Bot size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          {formatNumber(run.llm_calls)} model calls
        </span>
        <span className="mono">
          <GitBranch size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
          {run.repository}
        </span>
      </div>

      {(warnings.length > 0 || run.status === 'success_with_warnings') && (
        <div
          className="c"
          style={{
            border: '1px solid rgba(251, 191, 36, 0.4)',
            background: 'rgba(251, 191, 36, 0.08)',
            padding: '16px 22px',
            marginBottom: 20,
            borderRadius: 12,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: '#fbbf24', fontWeight: 600, fontSize: 14 }}>
            <AlertTriangle size={18} />
            <span>{notification || 'Execution Completed With Warnings'}</span>
          </div>
          {warnings.length > 0 && (
            <ul style={{ margin: '10px 0 0 24px', padding: 0, fontSize: 13, color: 'rgba(255,255,255,0.85)' }}>
              {warnings.map((w: string, idx: number) => (
                <li key={idx} style={{ marginBottom: 4 }}>{w}</li>
              ))}
            </ul>
          )}
          {(checksPassed.length > 0 || checksSkipped.length > 0) && (
            <div style={{ display: 'flex', gap: 16, marginTop: 12, fontSize: 12, flexWrap: 'wrap' }}>
              {checksPassed.length > 0 && (
                <div style={{ color: '#4ade80' }}>
                  ✓ Passed ({checksPassed.length}): {checksPassed.join(', ')}
                </div>
              )}
              {checksSkipped.length > 0 && (
                <div style={{ color: 'rgba(255,255,255,0.6)' }}>
                  ⊘ Skipped ({checksSkipped.length}): {checksSkipped.join(', ')}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <div className="gr c2">
        <div className="c">
          <div style={{ display: 'flex', gap: 10, borderBottom: '1px solid rgba(255,255,255,0.12)', paddingBottom: 14, marginBottom: 18 }}>
            {(['timeline', 'agents', 'diff', 'metrics'] as const).map((t) => (
              <button
                key={t}
                className={`bt sm ${tab === t ? '' : 'secondary'}`}
                onClick={() => setTab(t)}
              >
                {t === 'timeline' ? 'Event timeline' : t === 'agents' ? 'Agent outputs' : t === 'diff' ? 'Code diff' : 'Run metrics'}
              </button>
            ))}
          </div>

          {tab === 'timeline' && (
            <div className="timeline">
              {events.map((ev) => (
                <button
                  className={`timeline-event ${detail?.id === ev.id ? 'selected' : ''}`}
                  key={ev.id}
                  onClick={() => setDetail(ev)}
                >
                  <span
                    className={`timeline-node ${
                      ev.event_type.includes('error')
                        ? 'bad'
                        : ev.event_type.includes('completed') || ev.event_type === 'run_finished'
                        ? 'good'
                        : ''
                    }`}
                  />
                  <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                      <strong style={{ fontSize: 14 }}>{ev.message}</strong>
                      <span className="sb" style={{ fontSize: 12 }}>
                        {new Date(ev.timestamp).toLocaleTimeString()}
                      </span>
                    </div>
                    <div className="sb" style={{ fontSize: 12 }}>
                      {ev.agent || 'system'} · {ev.event_type}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}

          {tab === 'agents' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              {Object.entries(result.agents || {}).map(([role, output]: any) => (
                <details key={role} className="c" style={{ padding: 18 }} open={role === 'code_reviewer'}>
                  <summary style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, fontWeight: 700 }}>
                    <Bot size={16} />
                    <span>{role.replaceAll('_', ' ')}</span>
                    <span className="sb" style={{ marginLeft: 'auto', fontSize: 12 }}>
                      {result.agent_metrics?.[role]?.calls ?? 0} calls · {formatNumber(result.agent_metrics?.[role]?.total_tokens)} tokens
                    </span>
                  </summary>
                  <pre className="code-diff" style={{ marginTop: 14 }}>{output}</pre>
                </details>
              ))}
            </div>
          )}

          {tab === 'diff' && (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, fontWeight: 600 }}>
                <FileCode2 size={16} /> Final working-tree diff
              </div>
              {result.diff ? (
                <pre className="code-diff">{result.diff}</pre>
              ) : (
                <EmptyState title="No diff available yet" body="The code diff appears once the Coding Agent implements changes." />
              )}
            </div>
          )}

          {tab === 'metrics' && (
            <div className="gr e2">
              {Object.entries(run.metrics || {})
                .filter(([_k, v]) => typeof v !== 'object' && v !== null)
                .map(([k, v]) => (
                  <div className="c" key={k} style={{ padding: 18 }}>
                    <div className="lb">{k.replaceAll('_', ' ')}</div>
                    <div className="nm" style={{ fontSize: 32 }}>{String(v)}</div>
                  </div>
                ))}
            </div>
          )}
        </div>

        <div className="c">
          <h2>Event detail</h2>
          {detail ? (
            <div style={{ marginTop: 14 }}>
              <span className="pl">{detail.event_type}</span>
              <h3 style={{ margin: '14px 0 8px' }}>{detail.message}</h3>
              <div className="rw">
                <span className="lb">Timestamp</span>
                <span className="mono" style={{ fontSize: 13 }}>{new Date(detail.timestamp).toLocaleString()}</span>
              </div>
              <div className="rw">
                <span className="lb">Agent</span>
                <span className="mono" style={{ fontSize: 13 }}>{detail.agent || 'system'}</span>
              </div>
              <div style={{ marginTop: 14, marginBottom: 6, fontSize: 13, fontWeight: 600, color: 'rgba(255,255,255,0.7)' }}>
                Payload JSON
              </div>
              <pre className="code-diff" style={{ maxHeight: 220 }}>{JSON.stringify(detail.data, null, 2)}</pre>
            </div>
          ) : (
            <p className="sb" style={{ marginTop: 14 }}>Select a timeline event to view details.</p>
          )}

          <div style={{ marginTop: 24, paddingTop: 18, borderTop: '1px solid rgba(255,255,255,0.12)' }}>
            <h2>Verification Outcomes</h2>
            {(run.final_status || result.final_status) && (
              <div className="rw">
                <span className="lb">Final status</span>
                <StatusPill status={run.final_status || result.final_status} />
              </div>
            )}
            {(run.execution_status || result.execution_status) && (
              <div className="rw">
                <span className="lb">Execution</span>
                <span className="mono">{run.execution_status || result.execution_status}</span>
              </div>
            )}
            {(run.verification_status || result.verification_status) && (
              <div className="rw">
                <span className="lb">Verification</span>
                <span className="mono">{run.verification_status || result.verification_status}</span>
              </div>
            )}
            {(run.repository_health || result.repository_health) && (
              <div className="rw">
                <span className="lb">Repo health</span>
                <span className="mono">{run.repository_health || result.repository_health}</span>
              </div>
            )}
            {result.verification_state && (
              <div className="rw">
                <span className="lb">State</span>
                <span className="pl">{result.verification_state}</span>
              </div>
            )}
            <div className="rw">
              <span className="lb">Tests passed</span>
              <span className="mono">{result.test_passed ? 'Yes' : run.status === 'running' ? 'Pending' : 'No'}</span>
            </div>
            <div className="rw">
              <span className="lb">Review approved</span>
              <span className="mono">{result.review_approved ? 'Yes' : run.status === 'running' ? 'Pending' : 'No'}</span>
            </div>
            <div className="rw">
              <span className="lb">Changed files</span>
              <span className="mono">{formatNumber(result.changed_files?.length || 0)}</span>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

function RunHistory({
  runs,
  onOpen,
  onNavigate,
}: {
  runs: Run[]
  onOpen: (id: string) => void
  onNavigate: (p: Page) => void
}) {
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')

  const filtered = runs.filter(
    (r) =>
      (filter === 'all' || r.status === filter) &&
      `${r.repository} ${r.task} ${r.id}`.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <>
      <PageHeader
        eyebrow="Execution archive"
        title="Run history"
        description="Browse prior executions, inspect traces, and revisit verification outcomes."
        action={
          <Button onClick={() => onNavigate('run')}>
            <Plus size={15} /> New run
          </Button>
        }
      />

      <div className="c" style={{ padding: '14px 20px', marginBottom: 20 }}>
        <div style={{ display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            className="in"
            style={{ flex: 1, minWidth: 260, padding: '10px 16px' }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search repository, task, or run ID…"
          />
          <select
            className="in"
            style={{ width: 'auto', padding: '10px 16px' }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="all">All statuses</option>
            <option value="succeeded">Succeeded</option>
            <option value="success_with_warnings">Success with warnings</option>
            <option value="failed">Failed</option>
            <option value="timed_out">Timed out</option>
            <option value="cancelled">Cancelled</option>
            <option value="running">Running</option>
            <option value="queued">Queued</option>
          </select>
          <span className="mono" style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)' }}>
            {filtered.length} runs
          </span>
        </div>
      </div>

      <div className="gr" style={{ gap: 14 }}>
        {filtered.length ? (
          filtered.map((r) => (
            <div
              className="c clickable-row"
              key={r.id}
              onClick={() => onOpen(r.id)}
              style={{
                display: 'grid',
                gridTemplateColumns: '2.6fr 1fr 1fr 1fr',
                gap: 12,
                alignItems: 'center',
                padding: '20px 26px',
              }}
            >
              <div>
                <strong>{r.repository}</strong> <span className="sb">#{r.id}</span>
                <div className="sb" style={{ margin: '6px 0' }}>
                  {r.task.slice(0, 72)}...
                </div>
                <div className="sb" style={{ fontSize: 12 }}>
                  {new Date(r.created_at).toLocaleString()}
                </div>
              </div>
              <span className="pl" style={{ justifySelf: 'start' }}>
                <StatusPill status={r.status} />
              </span>
              <span className="mono" style={{ fontSize: 14 }}>
                {formatDuration(r.duration_ms)}
              </span>
              <span className="mono" style={{ fontSize: 13, color: 'rgba(255,255,255,0.7)' }}>
                {formatNumber(r.total_tokens)} tokens · {r.llm_calls} calls
              </span>
            </div>
          ))
        ) : (
          <EmptyState
            title="No matching runs"
            body="Start an evaluation to build your run history."
            action={<Button onClick={() => onNavigate('run')}>Start evaluation</Button>}
          />
        )}
      </div>
    </>
  )
}

function MetricsPage({
  metrics,
  runs,
  onNavigate,
}: {
  metrics: any
  runs: Run[]
  onNavigate: (p: Page) => void
}) {
  const completed = runs.filter((r) => r.status === 'succeeded' || r.status === 'failed')
  const data = completed
    .slice(0, 20)
    .reverse()
    .map((r, i) => ({
      run: `#${i + 1}`,
      tokens: r.total_tokens || 0,
      seconds: r.duration_ms ? Math.round(r.duration_ms / 1000) : 0,
    }))

  return (
    <>
      <PageHeader
        eyebrow="Engineering telemetry"
        title="Metrics & reports"
        description="Operational metrics calculated from actual recorded runs."
        action={
          <Button kind="secondary" onClick={() => onNavigate('history')}>
            View run history <ArrowRight size={15} />
          </Button>
        }
      />

      <div className="gr c4">
        <StatCard
          label="Verified success rate"
          value={metrics.success_rate == null ? '0%' : `${metrics.success_rate}%`}
          detail={`${metrics.successful_runs || 0} successful / ${metrics.completed_runs || 0} completed`}
        />
        <StatCard
          label="Avg wall-clock time"
          value={formatDuration(metrics.average_duration_ms)}
          detail={`p95 ${formatDuration(metrics.p95_duration_ms)}`}
        />
        <StatCard
          label="Input tokens"
          value={formatNumber(metrics.prompt_tokens || 0)}
          detail="Provider-reported prompt tokens"
        />
        <StatCard
          label="Output tokens"
          value={formatNumber(metrics.completion_tokens || 0)}
          detail="Provider-reported completion tokens"
        />
      </div>

      <div className="c" style={{ marginTop: 24 }}>
        <h2>Token use by completed run</h2>
        <div className="sb">Recent completed executions</div>
        {data.length ? (
          <div className="chart-wrap" style={{ height: 260 }}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data} margin={{ top: 12, right: 12, left: -10, bottom: 0 }}>
                <defs>
                  <linearGradient id="tokenGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#ffffff" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#ffffff" stopOpacity={0.01} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="4 6" stroke="rgba(255,255,255,.12)" vertical={false} />
                <XAxis dataKey="run" tick={{ fill: 'rgba(255,255,255,.6)', fontSize: 12, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: 'rgba(255,255,255,.6)', fontSize: 12, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
                <Tooltip
                  contentStyle={{
                    background: '#0d0d0d',
                    border: '1px solid rgba(255,255,255,0.2)',
                    borderRadius: 14,
                    color: '#ffffff',
                    fontFamily: 'Space Grotesk',
                  }}
                />
                <Area type="monotone" dataKey="tokens" name="Total tokens" stroke="#ffffff" strokeWidth={2.5} fill="url(#tokenGrad)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <EmptyState title="Metrics appear after runs" body="Run tasks to collect provider token data." />
        )}
      </div>

      <div className="gr e2" style={{ marginTop: 24 }}>
        <div className="c" style={{ padding: 22 }}>
          <b>Verified success</b>
          <p className="sb" style={{ margin: '8px 0 0' }}>
            A run is marked successful only if a configured test command passes and the code reviewer explicitly approves the diff.
          </p>
        </div>
        <div className="c" style={{ padding: 22 }}>
          <b>Wall-clock time</b>
          <p className="sb" style={{ margin: '8px 0 0' }}>
            Includes orchestration, provider latency, tool execution, tests, and review.
          </p>
        </div>
        <div className="c" style={{ padding: 22 }}>
          <b>Token counts</b>
          <p className="sb" style={{ margin: '8px 0 0' }}>
            Reported by the model API; can include repeated context and internal reasoning tokens.
          </p>
        </div>
        <div className="c" style={{ padding: 22 }}>
          <b>Important limitation</b>
          <p className="sb" style={{ margin: '8px 0 0' }}>
            Task success is a useful operational signal, not a guarantee of correctness.
          </p>
        </div>
      </div>
    </>
  )
}

function RepositoriesPage({
  repos,
  root,
  onRegistered,
  onNavigate,
  onSelectRepoForRun,
}: {
  repos: Repository[]
  root: string
  onRegistered: () => void
  onNavigate: (p: Page) => void
  onSelectRepoForRun: (r: string) => void
}) {
  const [tab, setTab] = useState<'github' | 'zip' | 'folder'>('github')
  const [gitUrl, setGitUrl] = useState('')
  const [gitBranch, setGitBranch] = useState('')
  const [gitName, setGitName] = useState('')

  const [zipFile, setZipFile] = useState<File | null>(null)
  const [zipName, setZipName] = useState('')

  const [folderPath, setFolderPath] = useState('')
  const [folderName, setFolderName] = useState('')

  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [busy, setBusy] = useState(false)
  const [busyMsg, setBusyMsg] = useState('')

  const handleClone = async () => {
    if (!gitUrl.trim()) {
      setError('Enter a valid GitHub or Git repository URL.')
      return
    }
    setError('')
    setSuccess('')
    setBusy(true)
    setBusyMsg('Cloning repository from Git...')
    try {
      const res = await api.cloneRepository(
        gitUrl.trim(),
        gitName.trim() || undefined,
        gitBranch.trim() || undefined
      )
      setGitUrl('')
      setGitBranch('')
      setGitName('')
      setSuccess(`Repository '${res.repository.name}' cloned successfully!`)
      onRegistered()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
      setBusyMsg('')
    }
  }

  const handleUploadZip = async () => {
    if (!zipFile) {
      setError('Select a .zip archive to upload.')
      return
    }
    setError('')
    setSuccess('')
    setBusy(true)
    setBusyMsg('Uploading and extracting zip archive...')
    try {
      const res = await api.uploadZip(zipFile, zipName.trim() || undefined)
      setZipFile(null)
      setZipName('')
      setSuccess(`Repository '${res.repository.name}' imported successfully from zip!`)
      onRegistered()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
      setBusyMsg('')
    }
  }

  const handleImportFolder = async () => {
    if (!folderPath.trim()) {
      setError('Enter the path to a local directory.')
      return
    }
    setError('')
    setSuccess('')
    setBusy(true)
    setBusyMsg('Importing and initializing folder...')
    try {
      const res = await api.importFolder(folderPath.trim(), folderName.trim() || undefined)
      setFolderPath('')
      setFolderName('')
      setSuccess(`Repository '${res.repository.name}' imported successfully!`)
      onRegistered()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
      setBusyMsg('')
    }
  }

  const handleDelete = async (name: string) => {
    if (!confirm(`Are you sure you want to remove '${name}' from workspace repositories?`)) return
    setError('')
    setSuccess('')
    try {
      await api.deleteRepository(name)
      setSuccess(`Repository '${name}' removed.`)
      onRegistered()
    } catch (e: any) {
      setError(e.message)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="Local workspaces"
        title="Repositories"
        description="Add GitHub repositories by link, upload .zip project archives, or import local folders. Each project gets an isolated Git baseline for automated evaluations."
      />

      {success && (
        <div className="success-banner">
          <CheckCircle2 size={16} />
          <span>{success}</span>
        </div>
      )}

      <div className="gr c2">
        <div className="c">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div>
              <h2>Available Repositories</h2>
              <div className="sb">{repos.length} ready for evaluation</div>
            </div>
            <GitBranch size={20} />
          </div>

          {repos.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {repos.map((r) => (
                <div className="rw" key={r.path} style={{ padding: '14px 16px', background: 'rgba(255,255,255,0.03)', borderRadius: 16 }}>
                  <div>
                    <strong style={{ fontSize: 16 }}>{r.name}</strong>
                    <div className="sb" style={{ fontSize: 12, marginTop: 4 }}>{r.path}</div>
                  </div>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button
                      className="bt sm"
                      onClick={() => {
                        onSelectRepoForRun(r.name)
                        onNavigate('run')
                      }}
                      title="Run evaluation on this repository"
                    >
                      <Play size={12} /> Run
                    </button>
                    <button
                      className="bt danger sm"
                      onClick={() => handleDelete(r.name)}
                      title="Remove repository"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              title="No repositories found"
              body="Use the panel on the right to clone a GitHub repo, upload a .zip, or import a folder."
            />
          )}
        </div>

        <div className="c">
          <h2>Add Repository</h2>
          <div className="sb" style={{ marginBottom: 16 }}>Choose how you'd like to import your repository</div>

          <div className="repo-tabs">
            <button
              className={`repo-tab ${tab === 'github' ? 'active' : ''}`}
              onClick={() => {
                setTab('github')
                setError('')
              }}
            >
              <GitBranch size={13} /> GitHub Link
            </button>
            <button
              className={`repo-tab ${tab === 'zip' ? 'active' : ''}`}
              onClick={() => {
                setTab('zip')
                setError('')
              }}
            >
              <FileArchive size={13} /> Upload .ZIP
            </button>
            <button
              className={`repo-tab ${tab === 'folder' ? 'active' : ''}`}
              onClick={() => {
                setTab('folder')
                setError('')
              }}
            >
              <FolderPlus size={13} /> Local folder
            </button>
          </div>

          {tab === 'github' && (
            <div>
              <label className="fl" style={{ marginTop: 0 }}>GitHub or Git repository URL</label>
              <input
                className="in"
                value={gitUrl}
                onChange={(e) => setGitUrl(e.target.value)}
                placeholder="https://github.com/owner/repository"
                disabled={busy}
              />
              <label className="fl">Branch / Tag (optional)</label>
              <input
                className="in"
                value={gitBranch}
                onChange={(e) => setGitBranch(e.target.value)}
                placeholder="main, master, v1.0 (default branch if blank)"
                disabled={busy}
              />
              <label className="fl">Custom name (optional)</label>
              <input
                className="in"
                value={gitName}
                onChange={(e) => setGitName(e.target.value)}
                placeholder="my-custom-repo-name"
                disabled={busy}
              />
              {error && <div className="inline-error">{error}</div>}
              <div style={{ marginTop: 20 }}>
                <Button onClick={handleClone} disabled={busy || !gitUrl.trim()}>
                  {busy ? busyMsg || 'Cloning...' : 'Clone repository'} <ArrowRight size={15} />
                </Button>
              </div>
            </div>
          )}

          {tab === 'zip' && (
            <div>
              <label className="fl" style={{ marginTop: 0 }}>Select .zip project archive</label>
              <label className="file-dropzone">
                <input
                  type="file"
                  accept=".zip"
                  style={{ display: 'none' }}
                  onChange={(e) => {
                    if (e.target.files?.[0]) {
                      setZipFile(e.target.files[0])
                      setError('')
                    }
                  }}
                  disabled={busy}
                />
                <div style={{ margin: '0 auto 8px' }}>
                  <UploadCloud size={24} />
                </div>
                <strong>{zipFile ? 'Click to change file' : 'Click or drop .zip file here'}</strong>
                <div className="sb" style={{ fontSize: 12, marginTop: 4 }}>Supports GitHub zip exports up to 250MB</div>
              </label>

              {zipFile && (
                <div className="file-selected">
                  <FileArchive size={16} />
                  <strong>{zipFile.name}</strong>
                  <span className="sb" style={{ marginLeft: 'auto' }}>
                    {(zipFile.size / 1024 / 1024).toFixed(2)} MB
                  </span>
                </div>
              )}

              <label className="fl">Custom repository name (optional)</label>
              <input
                className="in"
                value={zipName}
                onChange={(e) => setZipName(e.target.value)}
                placeholder="e.g. my-extracted-project"
                disabled={busy}
              />
              {error && <div className="inline-error">{error}</div>}
              <div style={{ marginTop: 20 }}>
                <Button onClick={handleUploadZip} disabled={busy || !zipFile}>
                  {busy ? busyMsg || 'Extracting...' : 'Upload & extract'} <ArrowRight size={15} />
                </Button>
              </div>
            </div>
          )}

          {tab === 'folder' && (
            <div>
              <label className="fl" style={{ marginTop: 0 }}>Local folder path</label>
              <input
                className="in"
                value={folderPath}
                onChange={(e) => setFolderPath(e.target.value)}
                placeholder="/Users/username/projects/my-app"
                disabled={busy}
              />
              <div className="sb" style={{ fontSize: 12, marginTop: 4 }}>
                Any local directory. It will be copied and initialized with Git.
              </div>

              <label className="fl">Custom name (optional)</label>
              <input
                className="in"
                value={folderName}
                onChange={(e) => setFolderName(e.target.value)}
                placeholder="e.g. imported-app"
                disabled={busy}
              />
              {error && <div className="inline-error">{error}</div>}
              <div style={{ marginTop: 20 }}>
                <Button onClick={handleImportFolder} disabled={busy || !folderPath.trim()}>
                  {busy ? busyMsg || 'Importing...' : 'Import folder'} <ArrowRight size={15} />
                </Button>
              </div>
            </div>
          )}

          <div className="sb" style={{ marginTop: 24, borderTop: '1px solid rgba(255,255,255,0.1)', paddingTop: 14 }}>
            <ShieldCheck size={15} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Repositories are automatically prepared with a Git baseline for isolated runs.
          </div>
        </div>
      </div>

      <div className="c" style={{ marginTop: 20, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <HardDrive size={18} />
          <span>Workspace repositories storage directory</span>
        </div>
        <span className="mono" style={{ fontSize: 13, color: 'rgba(255,255,255,0.7)' }}>
          {root || 'Loading…'}
        </span>
      </div>
    </>
  )
}

export default App
