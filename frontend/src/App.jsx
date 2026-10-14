import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { BarChart, Bar, PieChart, Pie, Cell, XAxis, YAxis, ResponsiveContainer, Tooltip } from 'recharts'
import LoginPage from './LoginPage'
import LandingPage from './LandingPage'

const SEV_COLORS = { Critical: '#dc2626', High: '#f59e0b', Medium: '#3b82f6', Low: '#64748b' }

function reasonMeta(reason) {
  const r = (reason || '').toLowerCase()
  if (r.includes('behind')) return { cls: 'behind', icon: 'ti-alert-triangle' }
  if (r.includes('ahead')) return { cls: 'ahead', icon: 'ti-arrow-up' }
  if (r.includes('blocked')) return { cls: 'blocked', icon: 'ti-lock' }
  if (r.includes('withdrawn')) return { cls: 'withdrawn', icon: 'ti-circle-x' }
  if (r.includes('unmapped')) return { cls: 'unmapped', icon: 'ti-help-circle' }
  return { cls: 'unmapped', icon: 'ti-alert-circle' }
}

function ReasonBadge({ reason }) {
  const meta = reasonMeta(reason)
  return (
    <span className={'reason ' + meta.cls}>
      <i className={'ti ' + meta.icon} aria-hidden="true"></i>{reason}
    </span>
  )
}

const TABS = {
  outcome: {
    label: 'Outcome', outcomeCombined: true,
    columns: ['Story', 'Story status', 'Outcome', 'Outcome status', 'Reason', 'Severity'],
    getRows: d => ({ leftKey: d.cr_key, leftStatus: d.cr_status, rightKey: d.story_key, rightStatus: d.story_status, reason: d.reason, severity: d.severity, score: d.score }),
    hasActions: true, dependentSide: 'right',
  },
  epic: {
    label: 'Epic', combined: true,
    columns: ['Epic', 'Epic status', 'Bottleneck CR', 'CR status', 'Reason', 'Severity'],
    getRows: d => ({ leftKey: d.story_key, leftStatus: d.story_status, rightKey: d.cr_key, rightStatus: d.cr_status, reason: d.reason, severity: d.severity, score: d.score }),
    hasActions: true, dependentSide: 'left',
  },
  cr: {
    label: 'CR', fieldTab: true,
    columns: ['CR', 'Status', 'Severity', 'Issues', 'Actions'],
  },
  story: {
    label: 'Story',
    columns: ['Story', 'Story status', 'CR', 'CR status', 'Reason', 'Severity'],
    getRows: d => ({ leftKey: d.story_key, leftStatus: d.story_status, rightKey: d.cr_key, rightStatus: d.cr_status, reason: d.reason, severity: d.severity, score: d.score }),
    hasActions: true, hasResolve: true, dependentSide: 'left',
  },
}

const OUTCOME_EPIC_CFG = {
  columns: ['Epic', 'Epic status', 'Outcome', 'Outcome status', 'Reason', 'Severity'],
  getRows: d => ({ leftKey: d.cr_key, leftStatus: d.cr_status, rightKey: d.story_key, rightStatus: d.story_status, reason: d.reason, severity: d.severity, score: d.score }),
  hasActions: true, dependentSide: 'left',
}

function getOutcomeTabCount(data) {
  return new Set([
    ...data.outcome.map(d => d.story_key).filter(Boolean),
    ...data.outcomeEpic.map(d => d.story_key).filter(Boolean),
    ...data.outcomeFields.map(f => f.entity_key).filter(Boolean),
  ]).size
}

function getOutcomePhaseRows(data) {
  return [
    ...data.outcomeEpic.map(OUTCOME_EPIC_CFG.getRows),
    ...data.outcome.map(TABS.outcome.getRows),
  ]
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts)
  return r.json()
}

function issueMatchesScope(scopeKeys, ...keys) {
  if (!scopeKeys || scopeKeys.size === 0) return true
  return keys.some(key => key && scopeKeys.has(key))
}

function getCurrentView() {
  const path = (window.location.pathname || '').toLowerCase()
  const hash = (window.location.hash || '').toLowerCase()
  if (path.endsWith('/dashboard') || hash === '#/dashboard') {
    return 'dashboard'
  }
  return 'landing'
}

export default function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [loading, setLoading] = useState(true)
  const [currentView, setCurrentView] = useState(getCurrentView())

  useEffect(() => {
    checkAuthentication()
  }, [])

  useEffect(() => {
    const syncView = () => setCurrentView(getCurrentView())
    window.addEventListener('hashchange', syncView)
    window.addEventListener('popstate', syncView)
    return () => {
      window.removeEventListener('hashchange', syncView)
      window.removeEventListener('popstate', syncView)
    }
  }, [])

  const checkAuthentication = async () => {
    try {
      const response = await fetch('/api/jira/check-session')
      const result = await response.json()
      setIsAuthenticated(result.authenticated || false)
    } catch (err) {
      console.error('Error checking authentication:', err)
      setIsAuthenticated(false)
    } finally {
      setLoading(false)
    }
  }

  const handleLoginSuccess = () => {
    window.location.hash = '/landing'
    setIsAuthenticated(true)
    setCurrentView('landing')
  }

  const handleLogout = async () => {
    try {
      await fetch('/api/jira/logout', { method: 'POST' })
      setIsAuthenticated(false)
      window.location.hash = ''
      setCurrentView('landing')
    } catch (err) {
      console.error('Logout error:', err)
    }
  }

  const openDashboard = () => {
    window.location.hash = '/dashboard'
    setCurrentView('dashboard')
  }

  const openLanding = () => {
    window.location.hash = '/landing'
    setCurrentView('landing')
  }

  if (loading) {
    return <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>Loading...</div>
  }

  if (!isAuthenticated) {
    return <LoginPage onLoginSuccess={handleLoginSuccess} />
  }

  if (currentView === 'dashboard') {
    return <Dashboard onLogout={handleLogout} onOpenLanding={openLanding} />
  }

  return <LandingPage onOpenDashboard={openDashboard} onLogout={handleLogout} />
}

function DashboardShell({ status, refreshing, errorMessage, searchValue, onSearchChange, onRefresh, onLogout, onOpenLanding, children }) {
  return (
    <div className="landing-shell dashboard-shell">
      <aside className="landing-sidebar">
        <div className="landing-brand">
          <div className="landing-brand-mark" aria-hidden="true">JC</div>
          <div>
            <strong>Jira Compliance &amp;</strong>
            <div>Delivery Control Center</div>
          </div>
        </div>

        <nav className="landing-nav">
          <button className="landing-nav-item active">Dashboard</button>
          <button className="landing-nav-item" onClick={onOpenLanding}>Compliance</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('dashboard-findings-panel')?.scrollIntoView({ behavior: 'smooth' })}>Traceability</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('dashboard-metrics-panel')?.scrollIntoView({ behavior: 'smooth' })}>Scrum dashboard</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('dashboard-findings-panel')?.scrollIntoView({ behavior: 'smooth' })}>Slot / PR control</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('dashboard-tabs-panel')?.scrollIntoView({ behavior: 'smooth' })}>Configuration</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('dashboard-findings-panel')?.scrollIntoView({ behavior: 'smooth' })}>Scan history</button>
        </nav>

        <div className="landing-connection-card">
          <div className="dot success"></div>
          <div>
            <strong>Connected to Jira</strong>
            <div>{status || 'Awaiting first scan'}</div>
            <small>Live workspace view with traceability insights</small>
          </div>
        </div>

        <div className="landing-quick-links">
          <p>Quick links</p>
          <button className="landing-link-button" onClick={onOpenLanding}>Open executive overview</button>
          <button className="landing-link-button" onClick={() => document.getElementById('dashboard-tabs-panel')?.scrollIntoView({ behavior: 'smooth' })}>Issue tabs</button>
          <button className="landing-link-button" onClick={() => document.getElementById('dashboard-findings-panel')?.scrollIntoView({ behavior: 'smooth' })}>Findings tables</button>
        </div>
      </aside>

      <main className="landing-main">
        <header className="landing-topbar">
          <div className="landing-search-wrap">
            <input
              className="landing-search"
              type="text"
              placeholder="Search issue key, summary, scrum team or reason"
              value={searchValue}
              onChange={event => onSearchChange(event.target.value)}
            />
          </div>
          <div className="landing-topbar-meta">
            <div className="landing-connection-pill"><span className="dot success"></span> Jira connected</div>
            <div className="landing-last-scan">{status || 'No scan yet'}</div>
            <button onClick={onOpenLanding}>Open executive overview</button>
            <button className="btn-primary" onClick={onRefresh}>{refreshing ? 'Re-scanning...' : 'Re-scan'}</button>
            <button onClick={onLogout}>Logout</button>
          </div>
        </header>

        {errorMessage && <section className="landing-status-banner error">{errorMessage}</section>}

        {children}
      </main>
    </div>
  )
}

function Dashboard({ onLogout, onOpenLanding }) {
  const [jiraBase, setJiraBase] = useState('')
  const [data, setData] = useState({ epic: [], epicFields: [], cr: [], story: [], outcome: [], outcomeFields: [], outcomeEpic: [] })
  const [summaries, setSummaries] = useState({})
  const [scrumTeams, setScrumTeams] = useState({})
  const [scrumTeamFilter, setScrumTeamFilter] = useState('all')
  const [status, setStatus] = useState('')
  const [summaryMeta, setSummaryMeta] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [autoScanAttempted, setAutoScanAttempted] = useState(false)
  const [dashboardSearch, setDashboardSearch] = useState('')
  const [currentTab, setCurrentTab] = useState('outcome')
  const [expandedKey, setExpandedKey] = useState(null)
  const [epicFieldsExpandedKey, setEpicFieldsExpandedKey] = useState(null)
  const [outcomeFieldsExpandedKey, setOutcomeFieldsExpandedKey] = useState(null)
  const [reasonFilter, setReasonFilter] = useState('all')
  const [fieldReasonFilter, setFieldReasonFilter] = useState('all')
  const [sevFilter, setSevFilter] = useState('all')
  const [sortAsc, setSortAsc] = useState(false)
  const [collapsedSections, setCollapsedSections] = useState({
    outcomeEpicStatusMismatches: false,
    epicFieldIssues: false,
    crFieldIssues: false,
    outcomeFieldIssues: false,
  })
  const [draft, setDraft] = useState(null)
  const [titleModal, setTitleModal] = useState(null)
  const [statusChange, setStatusChange] = useState(null)
  const [projects, setProjects] = useState([])
  const [boards, setBoards] = useState([])
  const [selectedProjectKey, setSelectedProjectKey] = useState('')
  const [selectedBoardId, setSelectedBoardId] = useState('')
  const [isLoadingBoards, setIsLoadingBoards] = useState(false)
  const [epicInput, setEpicInput] = useState('')
  const [epicExplorer, setEpicExplorer] = useState(null)
  const [epicLoading, setEpicLoading] = useState(false)
  const [epicError, setEpicError] = useState('')
  const fieldCache = useRef({})

  const loadAll = useCallback(async () => {
    try {
      const [summary, epicPairs, outcomePairs, outcomeEpicPairs, crFindings, epicFindings, outcomeFindings, summariesData, scrumTeamsData] = await Promise.all([
        fetchJSON('/api/dashboard/summary'),
        fetchJSON('/api/pairs/epic-cr'),
        fetchJSON('/api/pairs/story-outcome'),
        fetchJSON('/api/pairs/outcome-epic'),
        fetchJSON('/api/fields/findings?entity_type=cr'),
        fetchJSON('/api/fields/findings?entity_type=epic'),
        fetchJSON('/api/fields/findings?entity_type=outcome'),
        fetchJSON('/api/issues/summaries'),
        fetchJSON('/api/issues/scrum-teams'),
      ])
      setData({
        story: summary.active || [],
        epic: Array.isArray(epicPairs) ? epicPairs : [],
        epicFields: Array.isArray(epicFindings) ? epicFindings : [],
        outcome: Array.isArray(outcomePairs) ? outcomePairs : [],
        outcomeEpic: Array.isArray(outcomeEpicPairs) ? outcomeEpicPairs : [],
        cr: Array.isArray(crFindings) ? crFindings : [],
        outcomeFields: Array.isArray(outcomeFindings) ? outcomeFindings : [],
      })
      setSummaryMeta(summary)
      setSummaries(summariesData && typeof summariesData === 'object' ? summariesData : {})
      setScrumTeams(scrumTeamsData && typeof scrumTeamsData === 'object' ? scrumTeamsData : {})
      setStatus(summary.checked_at ? 'Last scan: ' + new Date(summary.checked_at * 1000).toLocaleString() : 'No scan yet')
      setErrorMessage(summary?.error || '')
      return summary
    } catch (err) {
      setErrorMessage(`Dashboard data could not be loaded: ${err.message}`)
      return null
    }
  }, [])

  useEffect(() => {
    fetchJSON('/api/config').then(c => {
      if (c.jira_base) setJiraBase(c.jira_base)
      loadProjects(c.default_project_key)
    })
  }, [])

  async function loadProjects(defaultKey) {
    const result = await fetchJSON('/api/jira/projects')
    if (result.error) { setProjects([]); return }
    const items = result.items || []
    setProjects(items)
    const upperDefault = (defaultKey || result.default_project_key || '').toUpperCase()
    const match = items.find(p => (p.key || '').toUpperCase() === upperDefault)
    const initial = match ? match.key : (items[0] ? items[0].key : '')
    setSelectedProjectKey(initial)
    if (initial) loadBoards(initial)
  }

  async function loadBoards(projectKey) {
    setIsLoadingBoards(true)
    setSelectedBoardId('')
    const result = await fetchJSON(`/api/jira/boards?project_key=${encodeURIComponent(projectKey)}`)
    if (!result.error) {
      setBoards(result.items || [])
      if (projectKey && projectKey.toUpperCase() === 'ACC' && result.default_board_id) {
        setSelectedBoardId(result.default_board_id)
      }
    } else {
      setBoards([])
    }
    setIsLoadingBoards(false)
  }

  function onProjectChange(key) {
    setSelectedProjectKey(key)
    setSelectedBoardId('')
    setStatus('Scope changed. Click Refresh now to rescan.')
    loadBoards(key)
  }

  function onBoardChange(id) {
    setSelectedBoardId(id)
    setStatus('Scope changed. Click Refresh now to rescan.')
  }

  useEffect(() => {
    if (!draft) return
    const onKey = e => { if (e.key === 'Escape') setDraft(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [draft])

  useEffect(() => {
    loadAll()
    const id = setInterval(loadAll, 60000)
    return () => clearInterval(id)
  }, [loadAll])

  useEffect(() => {
    if (!summaryMeta || autoScanAttempted || refreshing) return
    if (summaryMeta.run_id) return
    setAutoScanAttempted(true)
    refresh({ isBootstrap: true })
  }, [summaryMeta, autoScanAttempted, refreshing])

  async function refresh({ isBootstrap = false } = {}) {
    setRefreshing(true)
    setErrorMessage('')
    setStatus(isBootstrap
      ? 'No scan data found. Running the first compliance scan now...'
      : (selectedBoardId ? 'Scanning selected board...' : selectedProjectKey ? 'Scanning selected project...' : 'Scanning...'))
    try {
      const result = await fetchJSON('/api/dashboard/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_key: selectedBoardId ? null : (selectedProjectKey || null),
          board_id: selectedBoardId || null,
        }),
      })
      if (result?.error) {
        setErrorMessage(result.error)
        return
      }
      await loadAll()
    } finally {
      setRefreshing(false)
    }
  }

  async function loadEpicFocus(requestedEpicKey = epicInput) {
    const normalizedEpicKey = (requestedEpicKey || '').trim().toUpperCase()
    if (!normalizedEpicKey) {
      setEpicError('Enter an epic key such as ACC-123 to build the exact JQL and focus the dashboard.')
      setEpicExplorer(null)
      return
    }

    setEpicLoading(true)
    setEpicError('')
    setEpicInput(normalizedEpicKey)

    try {
      const result = await fetchJSON(`/api/hierarchy/epic-focus?epic_key=${encodeURIComponent(normalizedEpicKey)}`)
      if (result?.error) {
        setEpicExplorer(null)
        setEpicError(result.error)
        return
      }
      setEpicExplorer(result)
    } catch (err) {
      setEpicExplorer(null)
      setEpicError(`Epic lookup failed: ${err.message}`)
    } finally {
      setEpicLoading(false)
    }
  }

  function clearEpicFocus() {
    setEpicInput('')
    setEpicExplorer(null)
    setEpicError('')
  }

  async function resolveRow(storyKey, crKey) {
    await fetchJSON('/api/dashboard/resolve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ story_key: storyKey, cr_key: crKey }),
    })
    loadAll()
  }

  async function openTitleModal(key, fallbackSummary) {
    setTitleModal({ key, summary: fallbackSummary, description: null, loading: true, error: null })
    const result = await fetchJSON(`/api/issues/${key}/description`)
    if (result.error) {
      setTitleModal(m => (m && m.key === key) ? { ...m, loading: false, error: result.error } : m)
    } else {
      setTitleModal(m => (m && m.key === key)
        ? { ...m, loading: false, summary: result.summary || fallbackSummary, description: result.description }
        : m)
    }
  }

  async function draftPhaseComment(targetKey, targetStatus, otherKey, otherStatus, reason, severity) {
    setDraft({ forKey: targetKey, text: 'Generating draft with local LLM...', statusMsg: '' })
    const result = await fetchJSON('/api/ai/draft-comment', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'phase', target_key: targetKey, target_status: targetStatus, other_key: otherKey, other_status: otherStatus, reason, severity }),
    })
    if (result.error) setDraft({ forKey: targetKey, text: '', statusMsg: 'Error: ' + result.error })
    else setDraft({ forKey: targetKey, text: result.draft, statusMsg: '' })
  }

  async function draftFieldComment(entityKey, entityStatus, findings) {
    setDraft({ forKey: entityKey, text: 'Generating draft with local LLM...', statusMsg: '' })
    const result = await fetchJSON('/api/ai/draft-comment', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'fields', entity_key: entityKey, entity_status: entityStatus, findings }),
    })
    if (result.error) setDraft({ forKey: entityKey, text: '', statusMsg: 'Error: ' + result.error })
    else setDraft({ forKey: entityKey, text: result.draft, statusMsg: '' })
  }

  async function postDraft() {
    if (!draft) return
    setDraft(d => ({ ...d, statusMsg: 'Posting...' }))
    const result = await fetchJSON('/api/ai/post-comment', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ story_key: draft.forKey, comment: draft.text }),
    })
    setDraft(d => ({ ...d, statusMsg: result.error ? 'Error: ' + result.error : 'Posted ✓' }))
  }

  async function openStatusBox(issueKey) {
    setStatusChange({ forKey: issueKey, transitions: [], selected: '', msg: 'Loading available transitions...' })
    const result = await fetchJSON(`/api/jira/transitions/${issueKey}`)
    if (result.error) {
      setStatusChange({ forKey: issueKey, transitions: [], selected: '', msg: 'Error: ' + result.error })
      return
    }
    if (!result.transitions || result.transitions.length === 0) {
      setStatusChange({ forKey: issueKey, transitions: [], selected: '', msg: 'No transitions available from the current status.' })
      return
    }
    setStatusChange({ forKey: issueKey, transitions: result.transitions, selected: result.transitions[0].id, msg: '' })
  }

  function selectTransition(transitionId) {
    setStatusChange(s => ({ ...s, selected: transitionId }))
  }

  async function applyStatusChange() {
    if (!statusChange || !statusChange.selected) return
    setStatusChange(s => ({ ...s, msg: 'Applying...' }))
    const result = await fetchJSON('/api/jira/transition', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue_key: statusChange.forKey, transition_id: statusChange.selected }),
    })
    if (result.error) {
      setStatusChange(s => ({ ...s, msg: 'Error: ' + result.error }))
    } else {
      setStatusChange(s => ({ ...s, msg: 'Updated ✓' }))
      setTimeout(() => { setStatusChange(null); loadAll() }, 800)
    }
  }

  function switchTab(tab) {
    setCurrentTab(tab)
    setExpandedKey(null)
    setReasonFilter('all')
    setFieldReasonFilter('all')
    setSevFilter('all')
    setScrumTeamFilter('all')
  }

  function toggleSection(sectionKey) {
    setCollapsedSections(prev => ({ ...prev, [sectionKey]: !prev[sectionKey] }))
  }

  const cfg = TABS[currentTab]
  const epicFieldsCount = new Set([...data.epic.map(d => d.story_key), ...data.epicFields.map(f => f.entity_key)]).size
  const crCount = new Set(data.cr.map(f => f.entity_key)).size
  const outcomeCount = getOutcomeTabCount(data)
  const hasAnyDashboardData = Boolean(summaryMeta?.run_id) || [data.story, data.epic, data.cr, data.outcome, data.epicFields, data.outcomeFields, data.outcomeEpic].some(items => (items || []).length > 0)
  const epicScopeKeys = useMemo(
    () => (epicExplorer?.scope_keys?.length ? new Set(epicExplorer.scope_keys) : null),
    [epicExplorer],
  )

  return (
    <DashboardShell
      status={status || 'Loading...'}
      refreshing={refreshing}
      errorMessage={errorMessage}
      searchValue={dashboardSearch}
      onSearchChange={setDashboardSearch}
      onRefresh={refresh}
      onLogout={onLogout}
      onOpenLanding={onOpenLanding}
    >
      <section className="landing-hero dashboard-hero">
        <div>
          <h1>Operational compliance dashboard</h1>
          <p>
            Detailed working view for Outcome, Epic, CR, and Story compliance analysis, with faster triage, clearer traceability, and simpler operational actions.
          </p>
        </div>
        <div className="landing-hero-actions">
          <button className="btn-primary" onClick={onOpenLanding}>Open executive overview</button>
          <button onClick={refresh}>{refreshing ? 'Scanning...' : 'Refresh dashboard data'}</button>
        </div>
      </section>

      <div id="dashboard-tabs-panel" className="tabs dashboard-tabs-shell">
        {Object.entries(TABS).map(([key, t]) => {
          const count = key === 'epic' ? epicFieldsCount : key === 'cr' ? crCount : key === 'outcome' ? outcomeCount : data[key].length
          return (
            <button key={key} className={'tabbtn' + (currentTab === key ? ' active' : '')} onClick={() => switchTab(key)}>
              {t.label} <span className={'count' + (count > 0 ? ' has-issues' : '')}>{count}</span>
            </button>
          )
        })}
      </div>

      {!hasAnyDashboardData && !refreshing && (
        <section className="dashboard-empty-note">
          No scan data is loaded yet. Use <strong>Refresh dashboard data</strong> or start a scan from the executive overview.
        </section>
      )}

      <div id="dashboard-metrics-panel">
        <Cards tab={currentTab} data={data} />
        <Charts tab={currentTab} data={data} />
      </div>

      <div className="filters">
        <ReasonFilter tab={currentTab} data={data} value={reasonFilter} onChange={setReasonFilter} />
        {cfg.combined && <FieldReasonFilter findings={data.epicFields} value={fieldReasonFilter} onChange={setFieldReasonFilter} />}
        {cfg.outcomeCombined && <FieldReasonFilter findings={data.outcomeFields} value={fieldReasonFilter} onChange={setFieldReasonFilter} />}
        <select value={sevFilter} onChange={e => setSevFilter(e.target.value)}>
          <option value="all">All severities</option>
          <option>Critical</option><option>High</option><option>Medium</option><option>Low</option>
        </select>
        <select value={scrumTeamFilter} onChange={e => setScrumTeamFilter(e.target.value)}>
          <option value="all">All scrum teams</option>
          {[...new Set(Object.values(scrumTeams).filter(Boolean))].sort().map(team => (
            <option key={team} value={team}>{team}</option>
          ))}
        </select>
      </div>

      <EpicFocusPanel
        jiraBase={jiraBase}
        epicInput={epicInput}
        onEpicInputChange={setEpicInput}
        onLoadEpic={loadEpicFocus}
        onClearEpic={clearEpicFocus}
        epicExplorer={epicExplorer}
        epicLoading={epicLoading}
        epicError={epicError}
      />

      <div id="dashboard-findings-panel">
        {cfg.combined && data.epic.length > 0 && (
          <PhaseTable
            cfg={cfg} rows={data.epic} reasonFilter={reasonFilter} sevFilter={sevFilter}
            sortAsc={sortAsc} setSortAsc={setSortAsc} expandedKey={expandedKey} setExpandedKey={setExpandedKey}
            onResolve={resolveRow} onDraft={draftPhaseComment} onStatusChange={openStatusBox}
            statusChange={statusChange} onApplyStatus={applyStatusChange} onSelectTransition={selectTransition} onCloseStatus={() => setStatusChange(null)}
            title="Status mismatches" jiraBase={jiraBase} summaries={summaries} scrumTeams={scrumTeams} scrumTeamFilter={scrumTeamFilter} onTitleClick={openTitleModal}
            epicScopeKeys={epicScopeKeys} epicKey={epicExplorer?.epic_key}
          />
        )}
        {!cfg.combined && !cfg.fieldTab && !cfg.outcomeCombined && data[currentTab].length > 0 && (
          <PhaseTable
            cfg={cfg} rows={data[currentTab]} reasonFilter={reasonFilter} sevFilter={sevFilter}
            sortAsc={sortAsc} setSortAsc={setSortAsc} expandedKey={expandedKey} setExpandedKey={setExpandedKey}
            onResolve={resolveRow} onDraft={draftPhaseComment} onStatusChange={openStatusBox}
            statusChange={statusChange} onApplyStatus={applyStatusChange} onSelectTransition={selectTransition} onCloseStatus={() => setStatusChange(null)}
            title={'Non-compliant ' + currentTab + 's'} jiraBase={jiraBase} summaries={summaries} scrumTeams={scrumTeams} scrumTeamFilter={scrumTeamFilter} onTitleClick={openTitleModal}
            epicScopeKeys={epicScopeKeys} epicKey={epicExplorer?.epic_key}
          />
        )}
        {cfg.fieldTab && data.cr.length > 0 && (
          <FieldTable
            findings={data.cr} reasonFilter={reasonFilter} sevFilter={sevFilter}
            entityType="cr" expandedKey={expandedKey} setExpandedKey={setExpandedKey}
            onDraft={draftFieldComment} fieldCache={fieldCache} onSaved={loadAll}
            title="Non-compliant CRs" jiraBase={jiraBase} summaries={summaries} scrumTeams={scrumTeams} scrumTeamFilter={scrumTeamFilter} onTitleClick={openTitleModal}
            isCollapsed={collapsedSections.crFieldIssues} onToggleCollapse={() => toggleSection('crFieldIssues')}
            epicScopeKeys={epicScopeKeys} epicKey={epicExplorer?.epic_key}
          />
        )}
        {cfg.combined && data.epicFields.length > 0 && (
          <FieldTable
            findings={data.epicFields} reasonFilter={fieldReasonFilter} sevFilter={sevFilter}
            entityType="epic" expandedKey={epicFieldsExpandedKey} setExpandedKey={setEpicFieldsExpandedKey}
            onDraft={draftFieldComment} fieldCache={fieldCache} onSaved={loadAll}
            title="Field issues" jiraBase={jiraBase} summaries={summaries} scrumTeams={scrumTeams} scrumTeamFilter={scrumTeamFilter} onTitleClick={openTitleModal}
            isCollapsed={collapsedSections.epicFieldIssues} onToggleCollapse={() => toggleSection('epicFieldIssues')}
            epicScopeKeys={epicScopeKeys} epicKey={epicExplorer?.epic_key}
          />
        )}
        {cfg.outcomeCombined && (
          <>
            {data.outcomeEpic.length > 0 && <PhaseTable
              cfg={OUTCOME_EPIC_CFG} rows={data.outcomeEpic} reasonFilter={reasonFilter} sevFilter={sevFilter}
              sortAsc={sortAsc} setSortAsc={setSortAsc} expandedKey={expandedKey} setExpandedKey={setExpandedKey}
              onResolve={resolveRow} onDraft={draftPhaseComment} onStatusChange={openStatusBox}
              statusChange={statusChange} onApplyStatus={applyStatusChange} onSelectTransition={selectTransition} onCloseStatus={() => setStatusChange(null)}
              title="Epic Status mismatches" jiraBase={jiraBase} summaries={summaries} scrumTeams={scrumTeams} scrumTeamFilter={scrumTeamFilter} onTitleClick={openTitleModal}
              isCollapsed={collapsedSections.outcomeEpicStatusMismatches} onToggleCollapse={() => toggleSection('outcomeEpicStatusMismatches')}
              epicScopeKeys={epicScopeKeys} epicKey={epicExplorer?.epic_key}
            />}
            {data.outcome.length > 0 && <PhaseTable
              cfg={cfg} rows={data.outcome} reasonFilter={reasonFilter} sevFilter={sevFilter}
              sortAsc={sortAsc} setSortAsc={setSortAsc} expandedKey={expandedKey} setExpandedKey={setExpandedKey}
              onResolve={resolveRow} onDraft={draftPhaseComment} onStatusChange={openStatusBox}
              statusChange={statusChange} onApplyStatus={applyStatusChange} onSelectTransition={selectTransition} onCloseStatus={() => setStatusChange(null)}
              title="Story Status mismatches" jiraBase={jiraBase} summaries={summaries} scrumTeams={scrumTeams} scrumTeamFilter={scrumTeamFilter} onTitleClick={openTitleModal}
              epicScopeKeys={epicScopeKeys} epicKey={epicExplorer?.epic_key}
            />}
            {data.outcomeFields.length > 0 && <FieldTable
              findings={data.outcomeFields} reasonFilter={fieldReasonFilter} sevFilter={sevFilter}
              entityType="outcome" expandedKey={outcomeFieldsExpandedKey} setExpandedKey={setOutcomeFieldsExpandedKey}
              onDraft={draftFieldComment} fieldCache={fieldCache} onSaved={loadAll}
              title="Field issues" jiraBase={jiraBase} summaries={summaries} scrumTeams={scrumTeams} scrumTeamFilter={scrumTeamFilter} onTitleClick={openTitleModal}
              isCollapsed={collapsedSections.outcomeFieldIssues} onToggleCollapse={() => toggleSection('outcomeFieldIssues')}
              epicScopeKeys={epicScopeKeys} epicKey={epicExplorer?.epic_key}
            />}
          </>
        )}
      </div>

      {draft && (
        <div className="modal-overlay" onClick={() => setDraft(null)}>
          <div className="modal-card" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <p style={{ fontSize: 14, fontWeight: 500, margin: 0 }}>
                AI-drafted comment for <span style={{ color: '#2563eb', fontWeight: 700 }}>{draft.forKey}</span>
              </p>
              <button className="modal-close" onClick={() => setDraft(null)} aria-label="Close">x</button>
            </div>
            <p style={{ fontSize: 14, color: '#64748b', margin: '4px 0 10px' }}>Review before posting</p>
            <textarea value={draft.text} onChange={e => setDraft(d => ({ ...d, text: e.target.value }))} />
            <div className="actions">
              <button onClick={() => setDraft(null)}>Discard</button>
            </div>
            <p id="draftStatus">{draft.statusMsg}</p>
          </div>
        </div>
      )}

      {titleModal && (
        <div className="modal-overlay" onClick={() => setTitleModal(null)}>
          <div className="modal-card" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <p style={{ fontSize: 14, fontWeight: 500, margin: 0 }}>
                <span style={{ color: '#2563eb', fontWeight: 700 }}>{titleModal.key}</span>
              </p>
              <button className="modal-close" onClick={() => setTitleModal(null)} aria-label="Close">x</button>
            </div>
            <p style={{ fontSize: 15, fontWeight: 500, margin: '10px 0 4px' }}>{titleModal.summary || '(no title)'}</p>
            <div style={{ fontSize: 16, color: '#0f172a', marginTop: 10, whiteSpace: 'pre-wrap', maxHeight: 300, overflowY: 'auto' }}>
              {titleModal.loading && 'Loading description...'}
              {titleModal.error && <span style={{ color: '#dc2626', fontWeight: 700 }}>Error: {titleModal.error}</span>}
              {!titleModal.loading && !titleModal.error && (titleModal.description || <span style={{ color: '#64748b' }}>No description set.</span>)}
            </div>
          </div>
        </div>
      )}
    </DashboardShell>
  )
}

function ReasonFilter({ tab, data, value, onChange }) {
  const cfg = TABS[tab]
  let values
  if (cfg.fieldTab) {
    values = [...new Set(data.cr.map(f => f.field).filter(Boolean))]
  } else if (cfg.combined) {
    values = [...new Set(data.epic.map(cfg.getRows).map(r => r.reason).filter(Boolean))]
  } else if (cfg.outcomeCombined) {
    const epicPhaseReasons = data.outcomeEpic.map(OUTCOME_EPIC_CFG.getRows).map(r => r.reason).filter(Boolean)
    const storyPhaseReasons = data.outcome.map(cfg.getRows).map(r => r.reason).filter(Boolean)
    values = [...new Set([...epicPhaseReasons, ...storyPhaseReasons])]
  } else {
    values = [...new Set(data[tab].map(cfg.getRows).map(r => r.reason).filter(Boolean))]
  }
  return (
    <select value={value} onChange={e => onChange(e.target.value)}>
      <option value="all">All reasons</option>
      {values.map(v => <option key={v} value={v}>{v}</option>)}
    </select>
  )
}

function FieldReasonFilter({ findings, value, onChange }) {
  const values = [...new Set(findings.map(f => f.field).filter(Boolean))]
  return (
    <select value={value} onChange={e => onChange(e.target.value)}>
      <option value="all">All field issues</option>
      {values.map(v => <option key={v} value={v}>{v}</option>)}
    </select>
  )
}

function EpicFocusPanel({ jiraBase, epicInput, onEpicInputChange, onLoadEpic, onClearEpic, epicExplorer, epicLoading, epicError }) {
  const linkedParentByIssueKey = {}
  ;(epicExplorer?.issue_branches || []).forEach(branch => {
    ;(branch.children || []).forEach(issue => {
      linkedParentByIssueKey[issue.key] = branch.key
    })
  })

  const exactJql = epicExplorer?.jql?.exact || ''

  function issueStateClass(node) {
    if (!node) return 'neutral'
    if ((node.total_issue_count || 0) > 0) return (node.highest_severity || 'neutral').toLowerCase()
    if ((node.rule_coverage_count || 0) > 0) return 'good'
    return 'neutral'
  }

  function issueStateLabel(node) {
    if (!node) return 'No data'
    return node.compliance_label || 'No rule coverage'
  }

  function uniqueRuleLabels(node) {
    const labels = [...new Set((node?.compliance_rules || []).map(rule => rule.rule_label).filter(Boolean))]
    return labels.length ? labels.join(', ') : 'Not covered by current rule set'
  }

  function nodeIssueSummary(node) {
    if (!node) return 'No compliance details.'
    const statusCount = node.status_issue_count || 0
    const fieldCount = node.field_issue_count || 0
    if (!statusCount && !fieldCount) {
      return (node.rule_coverage_count || 0) > 0
        ? `All ${node.compliant_rule_count || 0}/${node.rule_coverage_count || 0} rules are compliant.`
        : 'No compliance rule applies to this issue yet.'
    }
    return `${statusCount} status issue${statusCount !== 1 ? 's' : ''} · ${fieldCount} field issue${fieldCount !== 1 ? 's' : ''}`
  }

  const EpicHierarchyBranch = ({ node, depth = 0 }) => {
    if (!node) return null
    const children = node.children || []
    const typeClass = node.is_cr
      ? 'cr'
      : (node.issue_type || '').toLowerCase() === 'epic'
        ? 'epic'
        : (node.issue_type || '').toLowerCase() === 'outcome'
          ? 'outcome'
          : 'leaf'

    return (
      <div className={`epic-tree-node ${depth === 0 ? 'branch-root' : 'leaf'}`}>
        <div className="epic-tree-node-header">
          <span className={`epic-type-pill ${typeClass}`}>{node.issue_type}</span>
          <a className="key-link" href={jiraBase + node.key} target="_blank" rel="noreferrer">{node.key}</a>
          <span>{node.summary}</span>
          <span className="epic-node-status">{node.status || 'Unknown'}</span>
          <span className={`tree-rate ${issueStateClass(node)}`}>{issueStateLabel(node)}</span>
          <span className="tree-meta-chip">{node.rule_coverage_count || 0} rules</span>
          <span className="tree-meta-chip">{node.total_issue_count || 0} open</span>
        </div>
        <div className="epic-tree-caption">{nodeIssueSummary(node)}</div>
        {children.length > 0 && (
          <div className="epic-tree-children compact">
            {children.map(child => <EpicHierarchyBranch key={child.key} node={child} depth={depth + 1} />)}
          </div>
        )}
      </div>
    )
  }

  return (
    <section className="landing-panel epic-focus-panel">
      <div className="landing-panel-header">
        <div>
          <h2>Epic issue explorer</h2>
          <p className="muted-copy">Build the exact JQL for a real epic key, focus the findings below, and show every issue in the epic with compliance-rule coverage and full hierarchy.</p>
        </div>
        {epicExplorer?.epic_key && <span className="breadcrumb-chip active">Focused on {epicExplorer.epic_key}</span>}
      </div>

      <div className="epic-focus-controls">
        <div className="control-group epic-focus-input-wrap">
          <label>Epic key</label>
          <input
            type="text"
            placeholder="ACC-123"
            value={epicInput}
            onChange={event => onEpicInputChange(event.target.value.toUpperCase())}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                onLoadEpic(epicInput)
              }
            }}
          />
        </div>
        <div className="epic-focus-actions">
          <button className="btn-primary" onClick={() => onLoadEpic(epicInput)} disabled={epicLoading}>
            {epicLoading ? 'Loading epic…' : 'Build JQL + hierarchy'}
          </button>
          <button onClick={onClearEpic} disabled={!epicExplorer && !epicInput}>Clear</button>
        </div>
      </div>

      {epicError && <div className="landing-error-banner" style={{ marginTop: 12, marginBottom: 0 }}>{epicError}</div>}

      {epicExplorer && (
        <div className="epic-focus-results">
          <div className="landing-data-notes epic-focus-note">
            The findings tables below are filtered to this epic scope. Epic issues now include both direct Jira “issues in epic” results and scan-derived CR/Story relationships so all issues against the epic are visible with compliance-rule coverage.
          </div>

          <div className="epic-focus-stats">
            <div className="rule-metric-card"><div className="kpi-label">Issues against epic</div><div className="kpi-value">{epicExplorer.summary?.direct_issue_count || 0}</div></div>
            <div className="rule-metric-card"><div className="kpi-label">Hierarchy branches</div><div className="kpi-value">{epicExplorer.summary?.branch_count || 0}</div></div>
            <div className="rule-metric-card"><div className="kpi-label">Linked nested tickets</div><div className="kpi-value">{epicExplorer.summary?.linked_issue_count || 0}</div></div>
            <div className="rule-metric-card"><div className="kpi-label">Direct issues with findings</div><div className="kpi-value">{epicExplorer.summary?.direct_issue_open_count || 0}</div></div>
          </div>

          <div className="epic-jql-card">
            <div className="epic-jql-header">
              <strong>Exact JQL for this epic</strong>
              <button type="button" onClick={() => {
                const copyRequest = navigator.clipboard?.writeText(exactJql)
                if (copyRequest?.catch) copyRequest.catch(() => {})
              }}>Copy JQL</button>
            </div>
            <code>{exactJql}</code>
            <div className="epic-jql-meta">
              <span>CR identification: <code>{epicExplorer.jql?.cr_only}</code></span>
              <span>Nested linked lookup: <code>{epicExplorer.jql?.linked_template}</code></span>
              <span>Query time: <strong>{epicExplorer.summary?.query_time_ms || 0}ms</strong></span>
            </div>
          </div>

          <div className="epic-path-row">
            {epicExplorer.outcome?.key && <span className="breadcrumb-chip">{epicExplorer.outcome.key}</span>}
            {epicExplorer.outcome?.key && <span className="epic-path-arrow">→</span>}
            <span className="breadcrumb-chip active">{epicExplorer.epic?.key}</span>
            <span className="muted-copy epic-path-summary">{epicExplorer.epic?.summary}</span>
            {epicExplorer.epic?.compliance_label && <span className={`tree-rate ${issueStateClass(epicExplorer.epic)}`}>{epicExplorer.epic.compliance_label}</span>}
          </div>

          <div className="epic-focus-grid">

            <div className="table-wrap">
              <div className="table-header"><p>Full hierarchy under the epic</p></div>
              <div className="epic-tree">
                {epicExplorer.outcome?.key && (
                  <div className="epic-tree-node outcome">
                    <div className="epic-tree-node-header">
                      <span className="epic-type-pill outcome">Outcome</span>
                      <a className="key-link" href={jiraBase + epicExplorer.outcome.key} target="_blank" rel="noreferrer">{epicExplorer.outcome.key}</a>
                      <span>{epicExplorer.outcome.summary}</span>
                      <span className="epic-node-status">{epicExplorer.outcome.status || 'Unknown'}</span>
                      <span className={`tree-rate ${issueStateClass(epicExplorer.outcome)}`}>{issueStateLabel(epicExplorer.outcome)}</span>
                    </div>
                    <div className="epic-tree-caption">{nodeIssueSummary(epicExplorer.outcome)}</div>
                  </div>
                )}

                <div className="epic-tree-node epic">
                  <div className="epic-tree-node-header">
                    <span className="epic-type-pill epic">Epic</span>
                    <a className="key-link" href={jiraBase + epicExplorer.epic.key} target="_blank" rel="noreferrer">{epicExplorer.epic.key}</a>
                    <span>{epicExplorer.epic.summary}</span>
                    <span className="epic-node-status">{epicExplorer.epic.status || 'Unknown'}</span>
                    <span className={`tree-rate ${issueStateClass(epicExplorer.epic)}`}>{issueStateLabel(epicExplorer.epic)}</span>
                    <span className="tree-meta-chip">{epicExplorer.issue_branches?.length || 0} branches</span>
                  </div>
                  <div className="epic-tree-caption">{nodeIssueSummary(epicExplorer.epic)}</div>

                  <div className="epic-tree-children">
                    {epicExplorer.issue_branches?.length ? epicExplorer.issue_branches.map(branch => (
                      <EpicHierarchyBranch key={branch.key} node={branch} />
                    )) : (
                      <div className="empty epic-tree-empty">No epic-child hierarchy branches were found for this epic.</div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

const PAGE_SIZE = 10

function Pagination({ page, setPage, totalItems, pageSize = PAGE_SIZE }) {
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize))
  if (totalPages <= 1) return null
  const start = (page - 1) * pageSize + 1
  const end = Math.min(page * pageSize, totalItems)
  return (
    <div className="pagination">
      <span className="pagination-info">{start}-{end} of {totalItems}</span>
      <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}>Previous</button>
      <span className="pagination-page">Page {page} of {totalPages}</span>
      <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page === totalPages}>Next</button>
    </div>
  )
}

function Cards({ tab, data }) {
  const cfg = TABS[tab]
  let checked, nonCompliant, critHigh, total
  if (cfg.fieldTab) {
    const entities = new Set(data.cr.map(f => f.entity_key))
    checked = entities.size; nonCompliant = entities.size
    critHigh = data.cr.filter(f => f.severity === 'High').length
    total = data.cr.length
  } else if (cfg.outcomeCombined) {
    const phaseRows = getOutcomePhaseRows(data)
    const checkedKeys = new Set([
      ...phaseRows.map(r => r.rightKey).filter(Boolean),
      ...data.outcomeFields.map(f => f.entity_key).filter(Boolean),
    ])
    const nonCompliantKeys = new Set([
      ...phaseRows.filter(r => r.severity).map(r => r.rightKey).filter(Boolean),
      ...data.outcomeFields.map(f => f.entity_key).filter(Boolean),
    ])
    checked = checkedKeys.size
    nonCompliant = nonCompliantKeys.size
    critHigh = phaseRows.filter(r => r.severity === 'Critical' || r.severity === 'High').length +
      data.outcomeFields.filter(f => f.severity === 'Critical' || f.severity === 'High').length
    total = phaseRows.length + data.outcomeFields.length
  } else {
    const rows = (cfg.combined ? data.epic : data[tab]).map(cfg.getRows)
    checked = rows.length; nonCompliant = rows.length
    critHigh = rows.filter(r => r.severity === 'Critical' || r.severity === 'High').length
    total = rows.length
  }
  return (
    <div className="cards">
      <div className="card"><p className="label">{cfg.fieldTab ? 'Entities with issues' : 'Checked'}</p><p className="value">{checked}</p></div>
      <div className="card danger"><p className="label">Non-compliant</p><p className="value">{nonCompliant}</p></div>
      <div className="card"><p className="label">Critical/High</p><p className="value">{critHigh}</p></div>
      <div className="card"><p className="label">Total findings</p><p className="value">{total}</p></div>
    </div>
  )
}

function Charts({ tab, data }) {
  const cfg = TABS[tab]
  let breakdown, sevData
  if (cfg.fieldTab) {
    const byField = {}
    data.cr.forEach(f => { byField[f.field] = (byField[f.field] || 0) + 1 })
    breakdown = Object.entries(byField).map(([name, value]) => ({ name, value }))
    const sevCounts = { Critical: 0, High: 0, Medium: 0, Low: 0 }
    data.cr.forEach(f => { if (sevCounts[f.severity] !== undefined) sevCounts[f.severity]++ })
    sevData = Object.entries(sevCounts).map(([name, value]) => ({ name, value }))
  } else if (cfg.outcomeCombined) {
    const phaseRows = getOutcomePhaseRows(data)
    const byReason = {}
    phaseRows.filter(r => r.reason).forEach(r => { byReason[r.reason] = (byReason[r.reason] || 0) + 1 })
    data.outcomeFields.filter(f => f.field).forEach(f => {
      const label = `Field: ${f.field}`
      byReason[label] = (byReason[label] || 0) + 1
    })
    breakdown = Object.entries(byReason).map(([name, value]) => ({ name, value }))
    const sevCounts = { Critical: 0, High: 0, Medium: 0, Low: 0 }
    phaseRows.forEach(r => { if (r.severity && sevCounts[r.severity] !== undefined) sevCounts[r.severity]++ })
    data.outcomeFields.forEach(f => { if (sevCounts[f.severity] !== undefined) sevCounts[f.severity]++ })
    sevData = Object.entries(sevCounts).map(([name, value]) => ({ name, value }))
  } else {
    const rows = (cfg.combined ? data.epic : data[tab]).map(cfg.getRows)
    const byReason = {}
    rows.forEach(r => { byReason[r.reason] = (byReason[r.reason] || 0) + 1 })
    breakdown = Object.entries(byReason).map(([name, value]) => ({ name, value }))
    const sevCounts = { Critical: 0, High: 0, Medium: 0, Low: 0 }
    rows.forEach(r => { if (sevCounts[r.severity] !== undefined) sevCounts[r.severity]++ })
    sevData = Object.entries(sevCounts).map(([name, value]) => ({ name, value }))
  }
  return (
    <div className="charts">
      <div className="chart-box">
        <p className="title">{cfg.fieldTab ? 'Findings by field' : 'Reason breakdown'}</p>
        <ResponsiveContainer width="100%" height="85%">
          <BarChart data={breakdown}>
            <XAxis dataKey="name" tick={{ fontSize: 10 }} interval={0} angle={-20} textAnchor="end" height={40} />
            <YAxis allowDecimals={false} tick={{ fontSize: 10 }} />
            <Tooltip />
            <Bar dataKey="value" fill="#2a78d6" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="chart-box">
        <p className="title">Severity distribution</p>
        <ResponsiveContainer width="100%" height="85%">
          <PieChart>
            <Pie data={sevData} dataKey="value" nameKey="name" innerRadius={35} outerRadius={60}>
              {sevData.map((entry, i) => <Cell key={i} fill={SEV_COLORS[entry.name]} />)}
            </Pie>
            <Tooltip />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

function SectionToggle({ isCollapsed, onToggleCollapse }) {
  if (!onToggleCollapse) return null
  return (
    <button type="button" className="btn-header" onClick={onToggleCollapse}>
      {isCollapsed ? 'Expand' : 'Collapse'}
    </button>
  )
}

function PhaseTable({ cfg, rows, reasonFilter, sevFilter, sortAsc, setSortAsc, expandedKey, setExpandedKey, onResolve, onDraft, onStatusChange, statusChange, onApplyStatus, onSelectTransition, onCloseStatus, title, jiraBase, summaries = {}, scrumTeams = {}, scrumTeamFilter = 'all', onTitleClick, isCollapsed = false, onToggleCollapse = null, epicScopeKeys = null, epicKey = '' }) {
  let filtered = rows.map(cfg.getRows)
    .filter(r => issueMatchesScope(epicScopeKeys, r.leftKey, r.rightKey, epicKey))
    .filter(r => (reasonFilter === 'all' || r.reason === reasonFilter) && (sevFilter === 'all' || r.severity === sevFilter))
    .filter(r => scrumTeamFilter === 'all' || scrumTeams[r.leftKey] === scrumTeamFilter || scrumTeams[r.rightKey] === scrumTeamFilter)
    .sort((a, b) => sortAsc ? (a.score || 0) - (b.score || 0) : (b.score || 0) - (a.score || 0))

  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1) }, [reasonFilter, sevFilter, scrumTeamFilter, rows, epicScopeKeys, epicKey])
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  const isTargetLeft = cfg.dependentSide !== 'right'

  return (
    <div className="table-wrap">
      <div className="table-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <p>{title}</p>
        <SectionToggle isCollapsed={isCollapsed} onToggleCollapse={onToggleCollapse} />
      </div>
      {!isCollapsed && <table>
        <colgroup>{cfg.columns.map((_, i) => <col key={i} style={{ width: (cfg.hasActions ? [12, 16, 12, 16, 24, 12] : [14, 16, 14, 16, 26, 14])[i] + '%' }} />)}{cfg.hasActions && <col style={{ width: '8%' }} />}<col style={{ width: '4%' }} /></colgroup>
        <thead>
          <tr>
            {cfg.columns.map(c => c === 'Severity'
              ? <th key={c} style={{ cursor: 'pointer' }} onClick={() => setSortAsc(!sortAsc)}>Severity</th>
              : <th key={c}>{c}</th>)}
            {cfg.hasActions && <th>Actions</th>}
            <th></th>
          </tr>
        </thead>
        <tbody>
          {filtered.length === 0 && (
            <tr><td colSpan={cfg.hasActions ? 7 : 6} className="empty">No mismatches match these filters.</td></tr>
          )}
          {pageRows.map(d => {
            const targetKey = isTargetLeft ? d.leftKey : d.rightKey
            const targetStatus = isTargetLeft ? d.leftStatus : d.rightStatus
            const otherKey = isTargetLeft ? d.rightKey : d.leftKey
            const otherStatus = isTargetLeft ? d.rightStatus : d.leftStatus
            const isOpen = expandedKey === d.leftKey
            return (
              <React.Fragment key={d.leftKey + d.rightKey}>
                <tr className="data-row" onClick={() => setExpandedKey(isOpen ? null : d.leftKey)}>
                  <td>
                    <a className="key-link" href={jiraBase + d.leftKey} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>{d.leftKey}</a>
                    {summaries[d.leftKey] && <div className="issue-title" onClick={e => { e.stopPropagation(); onTitleClick(d.leftKey, summaries[d.leftKey]) }}>{summaries[d.leftKey]}</div>}
                  </td>
                  <td>{d.leftStatus}</td>
                  <td>
                    <a className="key-link" href={jiraBase + d.rightKey} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>{d.rightKey}</a>
                    {summaries[d.rightKey] && <div className="issue-title" onClick={e => { e.stopPropagation(); onTitleClick(d.rightKey, summaries[d.rightKey]) }}>{summaries[d.rightKey]}</div>}
                  </td>
                  <td>{d.rightStatus}</td>
                  <td><ReasonBadge reason={d.reason} /></td>
                  <td><span className={'badge badge-' + d.severity}>{d.severity}</span></td>
                  {cfg.hasActions && (
                    <td onClick={e => e.stopPropagation()}>
                      <button onClick={() => onDraft(targetKey, targetStatus, otherKey, otherStatus, d.reason, d.severity)}>Draft</button>
                    </td>
                  )}
                  <td className="chevron">{isOpen ? '▾' : '▸'}</td>
                </tr>
                {statusChange && statusChange.forKey === targetKey && (
                  <tr className="status-inline-row">
                    <td colSpan={cfg.hasActions ? 7 : 6}>
                      <div className="status-inline">
                        <span style={{ fontSize: 16, fontWeight: 700, color: '#2563eb' }}>Change status for {targetKey}</span>
                        {statusChange.transitions.length > 0 ? (
                          <select value={statusChange.selected} onChange={e => onSelectTransition(e.target.value)}>
                            {statusChange.transitions.map(t => (
                              <option key={t.id} value={t.id}>{t.to_status} ({t.name})</option>
                            ))}
                          </select>
                        ) : (
                          <span style={{ fontSize: 14, color: '#64748b' }}>{statusChange.msg || 'Loading transitions...'}</span>
                        )}
                        {statusChange.transitions.length > 0 && <button className="btn-primary" onClick={onApplyStatus}>Apply</button>}
                        <button onClick={onCloseStatus}>Cancel</button>
                        {statusChange.msg && statusChange.transitions.length > 0 && (
                          <span style={{ fontSize: 14, color: '#64748b' }}>{statusChange.msg}</span>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
                {isOpen && (
                  <tr className="detail-row">
                    <td colSpan={cfg.hasActions ? 7 : 6}>
                      <div className="detail-inner">
                        <div className="detail-grid">
                          <div><span className="k">Reason</span><br /><ReasonBadge reason={d.reason} /></div>
                          <div><span className="k">Severity score</span><br />{d.score || 0} pts</div>
                        </div>
                        <div className="detail-actions">
                          {cfg.hasActions && <>
                            <button onClick={() => onDraft(targetKey, targetStatus, otherKey, otherStatus, d.reason, d.severity)}><i className="ti ti-message" style={{ fontSize: 14, marginRight: 6 }} aria-hidden="true"></i>Draft comment</button>
                          </>}
                          <a href={jiraBase + d.leftKey} target="_blank" rel="noreferrer">Open in Jira <i className="ti ti-external-link" style={{ fontSize: 14 }} aria-hidden="true"></i></a>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>}
      {!isCollapsed && <Pagination page={page} setPage={setPage} totalItems={filtered.length} />}
    </div>
  )
}

function FieldTable({ findings, reasonFilter, sevFilter, entityType, expandedKey, setExpandedKey, onDraft, fieldCache, onSaved, title, jiraBase, summaries = {}, scrumTeams = {}, scrumTeamFilter = 'all', onTitleClick, isCollapsed = false, onToggleCollapse = null, epicScopeKeys = null, epicKey = '' }) {
  const filtered = findings
    .filter(f => issueMatchesScope(epicScopeKeys, f.entity_key, epicKey))
    .filter(f => (reasonFilter === 'all' || f.field === reasonFilter) && (sevFilter === 'all' || f.severity === sevFilter))
    .filter(f => scrumTeamFilter === 'all' || scrumTeams[f.entity_key] === scrumTeamFilter)
  const rank = { Critical: 3, High: 2, Medium: 1, Low: 0 }
  const groups = {}
  filtered.forEach(f => {
    if (!groups[f.entity_key]) groups[f.entity_key] = { key: f.entity_key, status: f.entity_status, findings: [], maxRank: 0 }
    groups[f.entity_key].findings.push(f)
    groups[f.entity_key].maxRank = Math.max(groups[f.entity_key].maxRank, rank[f.severity] || 0)
  })
  const sevLabel = { 3: 'Critical', 2: 'High', 1: 'Medium', 0: 'Low' }
  const groupList = Object.values(groups).sort((a, b) => b.maxRank - a.maxRank)
  const entityLabel = { epic: 'Epic', outcome: 'Outcome' }[entityType] || 'CR'

  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1) }, [reasonFilter, sevFilter, scrumTeamFilter, findings, epicScopeKeys, epicKey])
  const pageGroups = groupList.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  return (
    <div className="table-wrap">
      <div className="table-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <p>{title}</p>
        <SectionToggle isCollapsed={isCollapsed} onToggleCollapse={onToggleCollapse} />
      </div>
      {!isCollapsed && <table>
        <colgroup><col style={{ width: '16%' }} /><col style={{ width: '18%' }} /><col style={{ width: '12%' }} /><col style={{ width: '26%' }} /><col style={{ width: '20%' }} /><col style={{ width: '8%' }} /></colgroup>
        <thead><tr><th>{entityLabel}</th><th>Status</th><th>Severity</th><th>Issues</th><th>Actions</th><th></th></tr></thead>
        <tbody>
          {groupList.length === 0 && <tr><td colSpan={6} className="empty">No findings.</td></tr>}
          {pageGroups.map(g => {
            const isOpen = expandedKey === g.key
            return (
              <React.Fragment key={g.key}>
                <tr className="data-row" onClick={() => setExpandedKey(isOpen ? null : g.key)}>
                  <td>
                    <a className="key-link" href={jiraBase + g.key} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>{g.key}</a>
                    {summaries[g.key] && <div className="issue-title" onClick={e => { e.stopPropagation(); onTitleClick(g.key, summaries[g.key]) }}>{summaries[g.key]}</div>}
                  </td>
                  <td>{g.status || '-'}</td>
                  <td><span className={'badge badge-' + sevLabel[g.maxRank]}>{sevLabel[g.maxRank]}</span></td>
                  <td>{g.findings.length} issue{g.findings.length > 1 ? 's' : ''}</td>
                  <td onClick={e => e.stopPropagation()}><button onClick={() => onDraft(g.key, g.status, g.findings)}>Draft</button></td>
                  <td className="chevron">{isOpen ? '▾' : '▸'}</td>
                </tr>
                {isOpen && <EditableFieldsRow entityKey={g.key} entityType={entityType} fieldCache={fieldCache} findings={g.findings} />}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>}
      {!isCollapsed && <Pagination page={page} setPage={setPage} totalItems={groupList.length} />}
    </div>
  )
}

function EditableFieldsRow({ entityKey, entityType, fieldCache, findings }) {
  const [fields, setFields] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    fetchJSON(`/api/entities/${entityType}/${entityKey}/fields`).then(result => {
      if (cancelled) return
      if (result.error) setError(result.error)
      else { setFields(result.fields); fieldCache.current[entityKey] = result.fields }
    })
    return () => { cancelled = true }
  }, [entityKey, entityType, fieldCache])

  const emptyCount = fields ? fields.filter(f => f.value === null || f.value === undefined || f.value === '').length : 0
  const flaggedCount = findings ? findings.length : 0

  const findingsByLabel = {}
  if (findings) {
    findings.forEach(f => {
      findingsByLabel[f.field] = findingsByLabel[f.field] || []
      findingsByLabel[f.field].push(f)
    })
  }

  return (
    <tr className="detail-row">
      <td colSpan={6}>
        <div style={{ padding: '4px 20px 16px 44px' }}>
          {error && <div>Error loading fields: {error}</div>}
          {!error && !fields && <div>Loading current field values...</div>}
          {fields && (
            <>
              <p style={{ fontSize: 14, color: '#64748b', margin: '0 0 8px' }}>
                Showing all {fields.length} fields - {emptyCount} empty and {flaggedCount} flagged issue{flaggedCount === 1 ? '' : 's'} below.
              </p>
              {fields.map(f => (
                <FieldEditor key={f.key} field={f} issues={findingsByLabel[f.label]} />
              ))}
            </>
          )}
        </div>
      </td>
    </tr>
  )
}

function FieldEditor({ field, issues }) {
  const isSet = field.value !== null && field.value !== undefined && field.value !== ''
  const displayValue = field.type === 'labels'
    ? (Array.isArray(field.value) ? field.value.join(', ') : '')
    : (field.value ?? '')

  const sevColor = { High: '#f59e0b', Medium: '#3b82f6', Low: '#64748b', Critical: '#dc2626' }
  const hasIssues = issues && issues.length > 0

  return (
    <div className="field-row" style={{ flexWrap: 'wrap' }}>
      <span className="field-label">{field.label}</span>
      <span className={'field-state ' + (isSet ? 'set' : 'unset')}>{isSet ? 'set' : 'not set'}</span>
      <span style={{ fontSize: 16, color: isSet ? '#0f172a' : '#64748b', fontStyle: isSet ? 'normal' : 'italic' }}>
        {isSet ? displayValue : 'Not set'}
      </span>
      {hasIssues && (
        <div style={{ width: '100%', paddingLeft: 240, marginTop: 2 }}>
          {issues.map((iss, i) => (
            <div key={i} style={{ fontSize: 12, color: sevColor[iss.severity] || '#888' }}>
              Warning: {iss.message}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
