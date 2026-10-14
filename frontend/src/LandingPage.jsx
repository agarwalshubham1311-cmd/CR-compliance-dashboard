import React, { useEffect, useMemo, useState } from 'react'
async function fetchJSON(url, opts) {
  const response = await fetch(url, opts)
  return response.json()
}
function statusTone(rate) {
  if (rate >= 90) return 'good'
  if (rate >= 70) return 'watch'
  return 'action'
}
function boolIcon(flag) {
  return flag ? '✓' : '—'
}
function getHierarchyMetaValue(node, keys) {
  for (const key of keys) {
    const value = node?.[key]
    if (value !== undefined && value !== null && value !== '') return value
  }
  return '—'
}
function getHierarchyRelationLabel(node) {
  const type = node?.type || 'Node'
  return type === 'CR/Story' ? 'CR / Story' : type
}
function getChildRelationLabel(parent, child) {
  return `${getHierarchyRelationLabel(parent)} → ${getHierarchyRelationLabel(child)}`
}
function collectNodeDetails(nodes, detailKey) {
  return (nodes || []).flatMap(node => (Array.isArray(node?.[detailKey]) ? node[detailKey] : []))
}
function getHierarchyAssignee(node) {
  return getHierarchyMetaValue(node, ['assignee', 'owner', 'responsible'])
}
function getIssueBadgeLabel(count, issueType) {
  if (count <= 0) return 'CLEAR'
  return `${count} ${issueType === 'status' ? 'STATUS' : 'FIELD'}`
}
function getIssueDialogLabel(issueType) {
  if (issueType === 'status') return 'Status mismatches'
  if (issueType === 'field') return 'Field mismatches'
  if (issueType === 'epic-issues') return 'Issues in epic'
  return 'Issues'
}
function getEpicIssueStateClass(node) {
  if (!node) return 'neutral'
  if ((node.total_issue_count || 0) > 0) return (node.highest_severity || 'neutral').toLowerCase()
  if ((node.rule_coverage_count || 0) > 0) return 'good'
  return 'neutral'
}
function getEpicIssueStateLabel(node) {
  if (!node) return 'No data'
  return node.compliance_label || 'No rule coverage'
}
function getEpicRuleLabels(node) {
  const labels = [...new Set((node?.compliance_rules || []).map(rule => rule.rule_label).filter(Boolean))]
  return labels.length ? labels.join(', ') : 'Not covered by current rule set'
}
function getEpicNodeIssueSummary(node) {
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
function sumNodeMetric(nodes, metricKey) {
  return (nodes || []).reduce((total, node) => total + (node?.[metricKey] || 0), 0)
}
function estimateNodeComplianceRate(issueCount, minimumIfIssues = 55) {
  if (!issueCount || issueCount <= 0) return 100
  return Math.max(minimumIfIssues, 100 - Math.min(48, issueCount * 8))
}
function buildHierarchyRoots(groups) {
  return (groups || []).map(group => {
    const children = group.outcomes || []
    const statusIssueDetails = collectNodeDetails(children, 'statusIssueDetails')
    const fieldIssueDetails = collectNodeDetails(children, 'fieldIssueDetails')
    const statusMismatchCount = statusIssueDetails.length || sumNodeMetric(children, 'statusMismatchCount')
    const fieldMissingCount = fieldIssueDetails.length || sumNodeMetric(children, 'fieldMissingCount')
    const issueCount = statusMismatchCount + fieldMissingCount
    return {
      key: group.key || `label:${group.label}`,
      label: group.label,
      summary: `${children.length} outcome${children.length === 1 ? '' : 's'}`,
      type: 'Label',
      pi: group.pi,
      assignee: group.assignee,
      status: group.status,
      statusMismatchCount,
      fieldMissingCount,
      issueCount,
      complianceRate: estimateNodeComplianceRate(issueCount),
      statusIssueDetails,
      fieldIssueDetails,
      children,
    }
  })
}
function updateRuleConfig(setConfigState, ruleKey, patch) {
  setConfigState(current => ({
    ...current,
    rules: (current.rules || []).map(rule => (rule.key === ruleKey ? { ...rule, ...patch } : rule)),
  }))
}
function updateFieldConfig(setConfigState, entityType, fieldKey, patch) {
  setConfigState(current => ({
    ...current,
    fields: {
      ...(current.fields || {}),
      [entityType]: ((current.fields || {})[entityType] || []).map(field => (field.key === fieldKey ? { ...field, ...patch } : field)),
    },
  }))
}
function collectEpicIssueRows(node, ancestry = []) {
  const children = Array.isArray(node?.children) ? node.children : []
  const rows = []

  for (const child of children) {
    const childRows = collectEpicIssueRows(child, [...ancestry, node.key])
    const fieldDetails = Array.isArray(child?.fieldIssueDetails) ? child.fieldIssueDetails : []
    const missingFields = fieldDetails
      .map(detail => detail?.field || detail?.message)
      .filter(Boolean)

    rows.push({
      issueKey: child.key,
      issueType: child.type,
      issueSummary: child.summary || child.key,
      issueStatus: child.status || '—',
      mandatoryFieldCount: fieldDetails.length,
      mandatoryFieldDetails: fieldDetails,
      missingFields,
      fieldCompliance: fieldDetails.length === 0 ? 'Compliant' : 'Needs attention',
      statusCompliance: (child.statusMismatchCount || 0) === 0 ? 'Compliant' : 'Needs attention',
      hasCompliance: fieldDetails.length === 0,
      hierarchyPath: [...ancestry, node.key, child.key].filter(Boolean).join(' → '),
      descendantCount: Array.isArray(child?.children) ? child.children.length : 0,
    })

    rows.push(...childRows)
  }

  return rows
}
function MetricBadge({ tone, label, interactive = false, onClick, title }) {
  if (interactive) {
    return (
      <button
        type="button"
        className={`tree-metric-badge tree-metric-badge--interactive ${tone}`}
        onClick={event => {
          event.stopPropagation()
          onClick?.(event)
        }}
        title={title}
      >
        {label}
      </button>
    )
  }

  return <span className={`tree-metric-badge ${tone}`}>{label}</span>
}
function JiraIssueLink({ jiraBase, issueKey, children, onClick }) {
  if (!jiraBase || !issueKey) return <span>{children || issueKey}</span>
  return (
    <a className="key-link" href={`${jiraBase}${issueKey}`} target="_blank" rel="noreferrer" onClick={onClick}>
      {children || issueKey}
    </a>
  )
}
export default function LandingPage({ onOpenDashboard, onLogout }) {
  const [overview, setOverview] = useState(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [savingConfig, setSavingConfig] = useState(false)
  const [statusMessage, setStatusMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [autoScanAttempted, setAutoScanAttempted] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [configState, setConfigState] = useState(null)
  const [search, setSearch] = useState('')
  const [selectedLabel, setSelectedLabel] = useState('all')
  const [selectedSeverity, setSelectedSeverity] = useState('all')
  const [selectedTeam, setSelectedTeam] = useState('all')
  const [selectedPI, setSelectedPI] = useState('all')
  const [compactMode, setCompactMode] = useState(true)
  const [jiraBase, setJiraBase] = useState('')
  const [scanState, setScanState] = useState(null)
  const [issueModal, setIssueModal] = useState(null)
  const loadOverview = async ({ silent = false } = {}) => {
    if (!silent && !overview) setLoading(true)
    try {
      const result = await fetchJSON('/api/landing/overview')
      if (result?.error) {
        setErrorMessage(`Landing overview could not be loaded: ${result.error}`)
        return null
      }
      setOverview(result)
      setConfigState(result.config)
      setErrorMessage('')
      return result
    } catch (error) {
      setErrorMessage(`Landing overview could not be loaded: ${error.message}`)
      return null
    } finally {
      if (!silent) setLoading(false)
    }
  }
  useEffect(() => {
    loadOverview()
    fetchJSON('/api/config')
      .then(result => {
        if (result?.jira_base) setJiraBase(result.jira_base)
      })
      .catch(() => {})
  }, [])
  useEffect(() => {
    if (!overview || autoScanAttempted || refreshing) return
    if (overview.summary?.run_id) return
    setAutoScanAttempted(true)
    triggerRefresh({ isBootstrap: true })
  }, [overview, autoScanAttempted, refreshing])
  useEffect(() => {
    if (!refreshing || !scanState) return
    let cancelled = false
    const pollStatus = async () => {
      try {
        const status = await fetchJSON('/api/dashboard/refresh-status')
        if (cancelled) return
        setScanState(status)
        if (status?.running) {
          return
        }
        const result = await loadOverview({ silent: true })
        if (cancelled) return
        if (status?.status === 'completed') {
          if (result?.summary?.run_id) {
            setStatusMessage(`Scan completed. ${result.kpis?.total_crs || 0} CRs and ${result.kpis?.total_stories || 0} stories are now loaded.`)
          } else {
            setStatusMessage('Background scan completed, but no Jira scan rows were stored yet. Check the backend scan logs for details.')
          }
        } else if (status?.status === 'failed') {
          setErrorMessage(`Scan failed: ${status.error || 'Unknown error'}`)
          setStatusMessage('')
        }
        setRefreshing(false)
      } catch (error) {
        if (cancelled) return
        setErrorMessage(`Scan status check failed: ${error.message}`)
        setStatusMessage('')
        setRefreshing(false)
      }
    }
    const timerId = window.setInterval(pollStatus, 2500)
    pollStatus()
    return () => {
      cancelled = true
      window.clearInterval(timerId)
    }
  }, [refreshing, scanState?.started_at])
  useEffect(() => {
    if (!issueModal) return undefined

    function handleEscape(event) {
      if (event.key === 'Escape') setIssueModal(null)
    }

    window.addEventListener('keydown', handleEscape)
    return () => window.removeEventListener('keydown', handleEscape)
  }, [issueModal])
  async function triggerRefresh({ isBootstrap = false } = {}) {
    setRefreshing(true)
    setErrorMessage('')
    setStatusMessage(isBootstrap ? 'No scan data found. Running the first compliance scan now...' : 'Running compliance scan and rebuilding landing data...')
    try {
      const refreshResult = await fetchJSON('/api/dashboard/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ background: true }),
      })
      if (refreshResult?.error) {
        setErrorMessage(`Scan failed: ${refreshResult.error}`)
        setStatusMessage('')
        setRefreshing(false)
        return
      }
      setScanState(refreshResult)
      setStatusMessage(
        isBootstrap
          ? 'Hierarchy is loading in the background. You can use the page immediately while the first scan completes.'
          : 'Full scan started in the background. Existing hierarchy data stays visible while results refresh.'
      )
    } catch (error) {
      setErrorMessage(`Scan failed: ${error.message}`)
      setStatusMessage('')
      setRefreshing(false)
    }
  }
  async function saveConfiguration() {
    if (!configState) return
    setSavingConfig(true)
    try {
      const saved = await fetchJSON('/api/configuration/compliance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(configState),
      })
      setConfigState(saved)
      setOverview(current => (current ? { ...current, config: saved } : current))
    } finally {
      setSavingConfig(false)
    }
  }
  function closeIssueModal() {
    setIssueModal(null)
  }
  function openIssueDetails(node, issueType, event) {
    event?.stopPropagation?.()
    if (!node) return

    const details = issueType === 'status'
      ? (node.statusIssueDetails || [])
      : (node.fieldIssueDetails || [])

    const issueCount = issueType === 'status'
      ? (node.statusMismatchCount || details.length || 0)
      : (node.fieldMissingCount || details.length || 0)

    if (issueType === 'epic-issues') {
      const epicDetails = collectEpicIssueRows(node)
      if (!epicDetails.length) return
      setIssueModal({
        nodeKey: node.key,
        nodeType: node.type,
        nodeSummary: node.summary,
        nodeStatus: node.status,
        issueType,
        issueCount: epicDetails.length,
        details: epicDetails,
        epicFocus: null,
      })
      return
    }

    if (!issueCount) return

    setIssueModal({
      nodeKey: node.key,
      nodeType: node.type,
      nodeSummary: node.summary,
      nodeStatus: node.status,
      issueType,
      issueCount,
      details,
    })
  }
  const hierarchySource = overview?.hierarchy || []
  const availableLabels = useMemo(
    () => (overview?.filters?.labels?.length ? overview.filters.labels : Array.from(new Set((hierarchySource || []).map(group => group.label).filter(Boolean)))),
    [overview?.filters?.labels, hierarchySource]
  )
  const availablePIs = useMemo(() => {
    const values = new Set()
    for (const group of hierarchySource || []) {
      if (group.pi) values.add(group.pi)
      for (const outcome of group.outcomes || []) {
        if (outcome.pi) values.add(outcome.pi)
      }
    }
    return Array.from(values).sort()
  }, [hierarchySource])
  const filteredHierarchy = useMemo(() => {
    if (!overview) return []
    const term = search.trim().toLowerCase()
    return (overview.hierarchy || [])
      .filter(group => selectedLabel === 'all' || group.label === selectedLabel)
      .map(group => {
        const outcomes = (group.outcomes || []).filter(outcome => {
          const matchesSearch = !term || [outcome.key, outcome.summary, ...(outcome.labels || [])].join(' ').toLowerCase().includes(term)
          const matchesTeam = selectedTeam === 'all' || outcome.scrumTeam === selectedTeam
          const matchesSeverity = selectedSeverity === 'all' || (outcome.issueCount || 0) > 0
          const outcomePI = outcome.pi || group.pi
          const matchesPI = selectedPI === 'all' || outcomePI === selectedPI
          return matchesSearch && matchesTeam && matchesSeverity && matchesPI
        })
        return { ...group, outcomes }
      })
      .filter(group => group.outcomes.length > 0)
  }, [overview, search, selectedLabel, selectedSeverity, selectedTeam, selectedPI])
  const hierarchyRoots = useMemo(() => buildHierarchyRoots(filteredHierarchy), [filteredHierarchy])
  const filteredIssues = useMemo(() => {
    if (!overview) return []
    const term = search.trim().toLowerCase()
    return (overview.recent_issues || [])
      .filter(item => selectedSeverity === 'all' || item.severity === selectedSeverity)
      .filter(item => selectedTeam === 'all' || item.owner === selectedTeam)
      .filter(item => !term || [item.key, item.summary, item.issueCategory, item.hierarchyPath].join(' ').toLowerCase().includes(term))
      .sort((a, b) => ({ Critical: 4, High: 3, Medium: 2, Low: 1 }[b.severity] || 0) - ({ Critical: 4, High: 3, Medium: 2, Low: 1 }[a.severity] || 0))
  }, [overview, search, selectedSeverity, selectedTeam])
  const summary = overview?.summary || {}
  const kpis = overview?.kpis || {}
  const hierarchyStatusCount = hierarchyRoots.reduce((total, node) => total + (node.statusMismatchCount || 0), 0)
  const hierarchyFieldCount = hierarchyRoots.reduce((total, node) => total + (node.fieldMissingCount || 0), 0)
  const outcomeCount = filteredHierarchy.reduce((total, group) => total + group.outcomes.length, 0)
  const hierarchySummaryCards = [
    { label: 'Labels', value: hierarchyRoots.length, helper: 'Business domains in view' },
    { label: 'Outcomes', value: outcomeCount, helper: 'Filtered hierarchy roots' },
    { label: 'Status mismatches', value: hierarchyStatusCount, helper: 'Needs phase alignment' },
    { label: 'Field gaps', value: hierarchyFieldCount, helper: 'Required governance updates' },
  ]
  const statusCards = [
    { key: 'overall_compliance', label: 'Overall compliance', value: `${kpis.overall_compliance || 0}%`, tone: statusTone(kpis.overall_compliance || 0), helper: `${kpis.total_stories || 0} stories scanned` },
    { key: 'status_mismatches', label: 'Status mismatches', value: kpis.status_mismatches || 0, tone: 'action', helper: 'Child status misaligned' },
    { key: 'field_gaps', label: 'Mandatory field gaps', value: kpis.field_gaps || 0, tone: (kpis.field_gaps || 0) > 0 ? 'watch' : 'good', helper: 'Configurable field rules' },
    { key: 'broken_hierarchy_links', label: 'Broken hierarchy links', value: kpis.broken_hierarchy_links || 0, tone: (kpis.broken_hierarchy_links || 0) > 0 ? 'watch' : 'good', helper: 'Outcome → Epic → CR checks' },
    { key: 'pr_missing_against_cr', label: 'PR missing against CR', value: kpis.pr_missing_against_cr || 0, tone: (kpis.pr_missing_against_cr || 0) > 0 ? 'action' : 'good', helper: 'Connector-ready metric' },
  ]
  const teamOptions = useMemo(() => {
    const teams = new Set()
    for (const item of overview?.recent_issues || []) {
      if (item.owner) teams.add(item.owner)
      if (item.scrumTeam) teams.add(item.scrumTeam)
    }
    return Array.from(teams).sort()
  }, [overview])
  const showInitialLoadingHint = loading && !overview && !errorMessage
  return (
    <div className="landing-shell">
      <aside className="landing-sidebar">
        <div className="landing-brand">
          <div className="landing-brand-mark" aria-hidden="true">JC</div>
          <div>
            <strong>Jira Compliance &amp;</strong>
            <div>Delivery Control Center</div>
          </div>
        </div>
        <nav className="landing-nav">
          <button className="landing-nav-item" onClick={onOpenDashboard}>Dashboard</button>
          <button className="landing-nav-item active">Compliance</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('traceability-panel')?.scrollIntoView({ behavior: 'smooth' })}>Traceability</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('matrix-panel')?.scrollIntoView({ behavior: 'smooth' })}>Compliance matrix</button>
          <button className="landing-nav-item" onClick={() => document.getElementById('issues-table-panel')?.scrollIntoView({ behavior: 'smooth' })}>Scan history</button>
          <button className="landing-nav-item" onClick={() => setConfigOpen(true)}>Configuration</button>
          {onLogout && <button className="landing-nav-item" onClick={onLogout}>Logout</button>}
        </nav>
        <div className="landing-connection-card">
          <div className="dot success"></div>
          <div>
            <strong>Connected to Jira</strong>
            <div>{summary.checked_at ? 'Scan data loaded' : 'Awaiting first scan'}</div>
            <small>Modern workspace with AI-assisted guidance</small>
          </div>
        </div>
      </aside>
      <main className="landing-main">
        <header className="landing-topbar">
          <div className="landing-search-wrap">
            <input
              className="landing-search"
              type="text"
              placeholder="Search issue key, summary, outcome or hierarchy path"
              value={search}
              onChange={event => setSearch(event.target.value)}
            />
          </div>
          <div className="landing-topbar-meta">
            <div className="landing-connection-pill"><span className="dot success"></span> Jira connected</div>
            <div className="landing-topbar-actions">
              <button onClick={() => triggerRefresh()}>{refreshing ? 'Scanning...' : 'Refresh scan'}</button>
              <button className="btn-primary" onClick={() => setConfigOpen(true)}>Manage rules</button>
            </div>
          </div>
        </header>
        {statusMessage && <div className="landing-status-banner">{statusMessage}</div>}
        {errorMessage && <div className="landing-error-banner">{errorMessage}</div>}
        {showInitialLoadingHint && <div className="dashboard-empty-note">Loading landing data…</div>}
        <section className="landing-hero">
          <div>
            <div className="eyebrow">Hierarchy traceability and compliance</div>
            <h1>Track delivery compliance across Labels, Outcomes, Epics, CRs and Stories.</h1>
            <p>
              Browse the saved hierarchy left to right for a faster, clearer view of Label → Outcome → Epic → CR / Story relationships and the compliance issues attached to each layer.
            </p>
          </div>
          <div className="landing-hero-actions">
            <button className="btn-primary" onClick={() => document.getElementById('traceability-panel')?.scrollIntoView({ behavior: 'smooth' })}>Open traceability</button>
            <button onClick={onOpenDashboard}>Open scrum dashboard</button>
            <button onClick={triggerRefresh}>{refreshing ? 'Scanning…' : 'Start new scan'}</button>
          </div>
        </section>
        <section className="landing-kpis">
          {statusCards.map(card => (
            <article key={card.key} className={`landing-kpi-card ${card.tone}`}>
              <p className="kpi-label">{card.label}</p>
              <p className="kpi-value">{card.value}</p>
              <p className="kpi-helper">{card.helper}</p>
            </article>
          ))}
        </section>
        <section className="landing-filterbar">
          <div className="control-group">
            <label>Label</label>
            <select value={selectedLabel} onChange={event => setSelectedLabel(event.target.value)}>
              <option value="all">All labels</option>
              {availableLabels.map(label => <option key={label} value={label}>{label}</option>)}
            </select>
          </div>
          <div className="control-group">
            <label>Scrum team</label>
            <select value={selectedTeam} onChange={event => setSelectedTeam(event.target.value)}>
              <option value="all">All scrum teams</option>
              {teamOptions.map(team => <option key={team} value={team}>{team}</option>)}
            </select>
          </div>
          <div className="control-group">
            <label>Criticality</label>
            <select value={selectedSeverity} onChange={event => setSelectedSeverity(event.target.value)}>
              <option value="all">All critical</option>
              {(overview?.filters?.severities || ['Critical', 'High', 'Medium', 'Low']).map(severity => <option key={severity} value={severity}>{severity}</option>)}
            </select>
          </div>
          <div className="control-group">
            <label>PI field</label>
            <select value={selectedPI} onChange={event => setSelectedPI(event.target.value)}>
              <option value="all">All PI field of outcomes</option>
              {availablePIs.map(pi => <option key={pi} value={pi}>{pi}</option>)}
            </select>
          </div>
          <div className="control-group filter-reset-wrap">
            <label>&nbsp;</label>
            <div className="button-group">
              <button onClick={() => { setSearch(''); setSelectedLabel('all'); setSelectedSeverity('all'); setSelectedTeam('all'); setSelectedPI('all'); setCompactMode(true) }}>Reset</button>
              <button className={compactMode ? 'active' : ''} onClick={() => setCompactMode(current => !current)}>{compactMode ? 'Compact mode on' : 'Compact mode off'}</button>
              <button onClick={() => setConfigOpen(true)}>More filters / rules</button>
            </div>
          </div>
        </section>

        <section className="landing-main-grid">
          <div id="traceability-panel" className={`landing-panel landing-hierarchy-panel ${compactMode ? 'compact-mode' : ''}`}>
            <div className="landing-panel-header">
              <h2>Hierarchy traceability &amp; compliance</h2>
              <span>{outcomeCount} outcomes • {hierarchyStatusCount} status mismatches • {hierarchyFieldCount} field gaps</span>
            </div>
            <p className="landing-section-intro">
              {compactMode
                ? 'Compact mode keeps the hierarchy within one screen by stacking outcomes vertically and keeping PI, assignee, ticket and status on a single line.'
                : 'The hierarchy is laid out in a horizontal board so you can compare PI, assignee and status on one line without opening the drill-down inspector.'}
            </p>
            <div className="landing-hierarchy-strip">
              {hierarchySummaryCards.map(card => (
                <article key={card.label} className="landing-hierarchy-strip-card">
                  <span>{card.label}</span>
                  <strong>{card.value}</strong>
                  <small>{card.helper}</small>
                </article>
              ))}
            </div>
            <div className="landing-hierarchy-tree-panel landing-hierarchy-tree-panel--full">
              <HierarchyColumnHeader />
              <div className="landing-hierarchy-tree">
                {hierarchyRoots.length === 0 && <p className="muted-copy">No hierarchy matches the active filters.</p>}
                {hierarchyRoots.map(group => (
                  <HierarchyGroup
                    key={group.key}
                    group={group}
                    jiraBase={jiraBase}
                    onOpenIssueDetails={openIssueDetails}
                  />
                ))}
              </div>
            </div>
          </div>
        </section>
        <section className="landing-secondary-grid">
          <div className="landing-panel landing-side-panel">
            <div className="landing-panel-header">
              <h2>Quick actions</h2>
            </div>
            <div className="landing-quick-actions-header">
              <h3>Keep this view current</h3>
              <p className="muted-copy">Run a fresh scan, jump to the detailed dashboard, or move directly to the active compliance items.</p>
            </div>
            <div className="landing-quick-actions">
              <button className="btn-primary" onClick={() => document.getElementById('traceability-panel')?.scrollIntoView({ behavior: 'smooth' })}>View traceability</button>
              <button onClick={onOpenDashboard}>Open scrum dashboard</button>
              <button onClick={triggerRefresh}>{refreshing ? 'Scanning...' : 'Start new scan'}</button>
              <button onClick={() => setConfigOpen(true)}>Edit rules &amp; fields</button>
            </div>
            <div className="landing-quick-action-status">
              <span className={`severity-pill ${refreshing ? 'Medium' : 'Low'}`}>{refreshing ? 'Scan running' : 'Using saved scan data'}</span>
              <span className="muted-copy small">{summary.checked_at ? `Last successful snapshot: ${new Date(summary.checked_at * 1000).toLocaleString()}` : 'A scan will populate this view automatically.'}</span>
            </div>
            <div className="landing-divider"></div>
            <div className="landing-mini-list">
              <h3>Recent issues</h3>
              {filteredIssues.slice(0, 5).map(item => (
                <div key={`${item.key}-${item.issueCategory}`} className="mini-issue-row">
                  <span className={`severity-pill ${item.severity}`}>{item.severity}</span>
                  <div>
                    <strong>{item.issueCategory}</strong>
                    <p><JiraIssueLink jiraBase={jiraBase} issueKey={item.key}>{item.key}</JiraIssueLink> — {item.summary}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>
        <section className="landing-bottom-grid">
          <div id="issues-table-panel" className="landing-panel landing-table-panel">
            <div className="landing-panel-header">
              <h2>Compliance issues</h2>
              <span>{filteredIssues.length} visible</span>
            </div>
            <div className="landing-table-wrap">
              <table className="landing-table">
                <thead>
                  <tr>
                    <th>Issue key</th>
                    <th>Type</th>
                    <th>Hierarchy path</th>
                    <th>Summary</th>
                    <th>Status</th>
                    <th>Compliance issue</th>
                    <th>Owner</th>
                    <th>Next action</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredIssues.length === 0 && (
                    <tr><td colSpan="8" className="empty">No issues match the active filters.</td></tr>
                  )}
                  {filteredIssues.slice(0, 12).map(item => (
                    <tr key={`${item.key}-${item.issueCategory}`}>
                      <td><JiraIssueLink jiraBase={jiraBase} issueKey={item.key}>{item.key}</JiraIssueLink></td>
                      <td>{item.type}</td>
                      <td>{item.hierarchyPath || '-'}</td>
                      <td>{item.summary}</td>
                      <td>{item.status || '-'}</td>
                      <td><span className={`severity-pill ${item.severity}`}>{item.issueCategory}</span></td>
                      <td>{item.owner}</td>
                      <td>{item.nextAction}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <div className="landing-side-stack">
            <div id="matrix-panel" className="landing-panel">
              <div className="landing-panel-header">
                <h2>Compliance matrix</h2>
              </div>
              <table className="landing-matrix-table">
                <thead>
                  <tr>
                    <th>Level</th>
                    <th>Status</th>
                    <th>Fields</th>
                    <th>Hierarchy</th>
                    <th>PR</th>
                    <th>Overall</th>
                  </tr>
                </thead>
                <tbody>
                  {(overview?.compliance_matrix || []).map(row => (
                    <tr key={row.level}>
                      <td>{row.level}</td>
                      <td className={row.status ? 'ok' : 'warn'}>{boolIcon(row.status)}</td>
                      <td className={row.fields ? 'ok' : 'warn'}>{boolIcon(row.fields)}</td>
                      <td className={row.hierarchy ? 'ok' : 'warn'}>{boolIcon(row.hierarchy)}</td>
                      <td className={row.pr ? 'ok' : 'warn'}>{boolIcon(row.pr)}</td>
                      <td>{row.overall}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div id="agentic-ai-panel" className="landing-panel">
              <div className="landing-panel-header">
                <h2>Agentic AI implementation</h2>
              </div>
              <div className="landing-agentic-list">
                {(overview?.agentic_ai?.stack || []).map(item => <span key={item} className="agentic-chip">{item}</span>)}
              </div>
              <ul className="landing-notes-list">
                {(overview?.agentic_ai?.notes || []).map(note => <li key={note}>{note}</li>)}
              </ul>
            </div>
            <div className="landing-panel">
              <div className="landing-panel-header">
                <h2>Configurable rules</h2>
              </div>
              <div className="landing-rules-grid">
                {(overview?.rule_metrics || []).filter(rule => rule.enabled).map(rule => (
                  <article key={rule.key} className="rule-metric-card">
                    <div>
                      <p>{rule.label}</p>
                      <strong>{rule.count}</strong>
                    </div>
                    <span className={`severity-pill ${rule.severity}`}>{rule.severity}</span>
                  </article>
                ))}
              </div>
            </div>
          </div>
        </section>
        {(overview?.data_notes || []).length > 0 && (
          <section className="landing-data-notes">
            {(overview.data_notes || []).map(note => <p key={note}>{note}</p>)}
          </section>
        )}
      </main>
      {issueModal && (
        <HierarchyIssueModal
          issueModal={issueModal}
          jiraBase={jiraBase}
          compactMode={compactMode}
          onClose={closeIssueModal}
        />
      )}
      {configOpen && configState && (
        <div className="landing-config-overlay" onClick={() => setConfigOpen(false)}>
          <div className="landing-config-drawer" onClick={event => event.stopPropagation()}>
            <div className="landing-panel-header">
              <h2>Compliance configuration</h2>
              <button onClick={() => setConfigOpen(false)}>Close</button>
            </div>
            <p className="muted-copy">
              These settings drive the landing page cards and field visibility so governance changes can be made without reworking the UI.
            </p>
            <section className="config-section">
              <h3>Rules</h3>
              {(configState.rules || []).map(rule => (
                <div key={rule.key} className="config-rule-row">
                  <label>
                    <input
                      type="checkbox"
                      checked={!!rule.enabled}
                      onChange={event => updateRuleConfig(setConfigState, rule.key, { enabled: event.target.checked })}
                    />
                    <span>{rule.label}</span>
                  </label>
                  <select
                    value={rule.severity || 'Low'}
                    onChange={event => updateRuleConfig(setConfigState, rule.key, { severity: event.target.value })}
                  >
                    <option value="Critical">Critical</option>
                    <option value="High">High</option>
                    <option value="Medium">Medium</option>
                    <option value="Low">Low</option>
                  </select>
                </div>
              ))}
            </section>
            <section className="config-section">
              <h3>Fields by entity</h3>
              {Object.entries(configState.fields || {}).map(([entityType, fields]) => (
                <div key={entityType} className="config-field-group">
                  <h4>{entityType.toUpperCase()}</h4>
                  {fields.map(field => (
                    <div key={`${entityType}-${field.key}`} className="config-field-row">
                      <div>
                        <strong>{field.label}</strong>
                        <div className="muted-copy small">{field.key}{field.editable === false ? ' • metadata only' : ''}</div>
                      </div>
                      <label>
                        <input
                          type="checkbox"
                          checked={!!field.visible}
                          onChange={event => updateFieldConfig(setConfigState, entityType, field.key, { visible: event.target.checked })}
                        />
                        Visible
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={!!field.required}
                          onChange={event => updateFieldConfig(setConfigState, entityType, field.key, { required: event.target.checked })}
                        />
                        Required
                      </label>
                    </div>
                  ))}
                </div>
              ))}
            </section>
            <div className="landing-config-actions">
              <button onClick={() => setConfigOpen(false)}>Cancel</button>
              <button className="btn-primary" onClick={saveConfiguration}>{savingConfig ? 'Saving...' : 'Save configuration'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
function EpicFocusDetails({ epicExplorer, jiraBase }) {
  const linkedParentByIssueKey = {}
  ;(epicExplorer?.issue_branches || []).forEach(branch => {
    ;(branch.children || []).forEach(issue => {
      linkedParentByIssueKey[issue.key] = branch.key
    })
  })
  const epicIssueRows = [...(epicExplorer?.direct_issues || [])].sort((a, b) => {
    const severityRank = value => ({ Critical: 4, High: 3, Medium: 2, Low: 1 }[value] || 0)
    return severityRank(b.highest_severity) - severityRank(a.highest_severity) || String(a.key || '').localeCompare(String(b.key || ''))
  })

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
          <JiraIssueLink jiraBase={jiraBase} issueKey={node.key}>{node.key}</JiraIssueLink>
          <span>{node.summary}</span>
          <span className="epic-node-status">{node.status || 'Unknown'}</span>
          <span className={`tree-rate ${getEpicIssueStateClass(node)}`}>{getEpicIssueStateLabel(node)}</span>
          <span className="tree-meta-chip">{node.rule_coverage_count || 0} rules</span>
          <span className="tree-meta-chip">{node.total_issue_count || 0} open</span>
        </div>
        <div className="epic-tree-caption">{getEpicNodeIssueSummary(node)}</div>
        {children.length > 0 && (
          <div className="epic-tree-children compact">
            {children.map(child => <EpicHierarchyBranch key={child.key} node={child} depth={depth + 1} />)}
          </div>
        )}
      </div>
    )
  }

  const EpicIssueCard = ({ issue, index }) => {
    const rules = Array.isArray(issue?.compliance_rules) ? issue.compliance_rules : []
    const statusIssues = Array.isArray(issue?.status_issues) ? issue.status_issues : []
    const fieldIssues = Array.isArray(issue?.field_issues) ? issue.field_issues : []
    const branchLabel = linkedParentByIssueKey[issue?.key] || (issue?.is_cr ? 'Epic root CR branch' : 'Epic root branch')

    return (
      <details className={`epic-issue-card ${issue?.is_compliant ? 'compliant' : 'needs-attention'}`} open={index < 2}>
        <summary className="epic-issue-card-summary">
          <div className="epic-issue-card-main">
            <div className="epic-issue-card-keyline">
              <JiraIssueLink jiraBase={jiraBase} issueKey={issue?.key}>{issue?.key}</JiraIssueLink>
              <span className="epic-type-pill leaf">{issue?.issue_type}</span>
              <span className="epic-node-status">{issue?.status || 'Unknown'}</span>
            </div>
            <div className="epic-issue-card-summary-text">{issue?.summary}</div>
            <div className="epic-issue-card-meta">{branchLabel}</div>
          </div>
          <div className="epic-issue-card-side">
            <span className={`tree-rate ${getEpicIssueStateClass(issue)}`}>{getEpicIssueStateLabel(issue)}</span>
            <span className="tree-meta-chip">{rules.length} rules</span>
            <span className="tree-meta-chip">{issue?.total_issue_count || 0} findings</span>
          </div>
        </summary>

        <div className="epic-issue-card-body">
          <div className="epic-issue-card-grid">
            <div className="epic-issue-card-section">
              <h4>Compliance rules</h4>
              {rules.length ? (
                <ul className="epic-issue-rule-list">
                  {rules.map((rule, ruleIndex) => (
                    <li key={`${issue?.key}-rule-${ruleIndex}`} className={`epic-issue-rule ${rule.compliant ? 'compliant' : 'open'}`}>
                      <div className="epic-issue-rule-header">
                        <strong>{rule.rule_label || rule.pair_type || 'Rule'}</strong>
                        <span className={`severity-pill ${rule.severity || 'Low'}`}>{rule.compliant ? 'Compliant' : rule.severity || 'Low'}</span>
                      </div>
                      <div className="epic-issue-rule-meta">
                        {rule.issue_label} → {rule.related_label} {rule.related_key ? `(${rule.related_key})` : ''}
                      </div>
                      <div className="epic-issue-rule-reason">{rule.reason || 'No rule reason captured.'}</div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="epic-issue-empty-note">No compliance rule is currently attached to this issue.</p>
              )}
            </div>

            <div className="epic-issue-card-section">
              <h4>Status findings</h4>
              {statusIssues.length ? (
                <ul className="epic-issue-finding-list">
                  {statusIssues.slice(0, 4).map((detail, detailIndex) => (
                    <li key={`${issue?.key}-status-${detailIndex}`}>
                      <strong>{detail.related_label} {detail.related_key}</strong>
                      <span className={`severity-pill ${detail.severity || 'Low'}`}>{detail.severity || 'Low'}</span>
                      <div>{detail.reason || 'No mismatch reason captured.'}</div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="epic-issue-empty-note">No status mismatches were found for this issue.</p>
              )}
            </div>

            <div className="epic-issue-card-section">
              <h4>Field findings</h4>
              {fieldIssues.length ? (
                <ul className="epic-issue-finding-list">
                  {fieldIssues.slice(0, 4).map((detail, detailIndex) => (
                    <li key={`${issue?.key}-field-${detailIndex}`}>
                      <strong>{detail.field || 'Unknown field'}</strong>
                      <span className={`severity-pill ${detail.severity || 'Low'}`}>{detail.severity || 'Low'}</span>
                      <div>{detail.message || 'No field mismatch reason captured.'}</div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="epic-issue-empty-note">No field issues were found for this issue.</p>
              )}
            </div>
          </div>
          <div className="epic-issue-card-footer">
            <span>Hierarchy branch</span>
            <strong>{branchLabel}</strong>
          </div>
        </div>
      </details>
    )
  }

  return (
    <div className="epic-focus-results landing-epic-focus-results">
      <div className="landing-data-notes epic-focus-note">
        Live epic scope uses the exact Jira “issues in epic” JQL plus saved scan-derived CR/Story relationships so the executive dashboard shows the complete hierarchy with compliance-rule coverage.
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
          <button
            type="button"
            onClick={() => {
              const copyRequest = navigator.clipboard?.writeText(epicExplorer?.jql?.exact || '')
              if (copyRequest?.catch) copyRequest.catch(() => {})
            }}
          >
            Copy JQL
          </button>
        </div>
        <code>{epicExplorer?.jql?.exact || 'No JQL returned.'}</code>
        <div className="epic-jql-meta">
          <span>CR identification: <code>{epicExplorer?.jql?.cr_only}</code></span>
          <span>Nested linked lookup: <code>{epicExplorer?.jql?.linked_template}</code></span>
          <span>Query time: <strong>{epicExplorer?.summary?.query_time_ms || 0}ms</strong></span>
        </div>
      </div>

      <div className="epic-path-row">
        {epicExplorer?.outcome?.key && <span className="breadcrumb-chip">{epicExplorer.outcome.key}</span>}
        {epicExplorer?.outcome?.key && <span className="epic-path-arrow">→</span>}
        <span className="breadcrumb-chip active">{epicExplorer?.epic?.key}</span>
        <span className="muted-copy epic-path-summary">{epicExplorer?.epic?.summary}</span>
        {epicExplorer?.epic?.compliance_label && (
          <span className={`tree-rate ${getEpicIssueStateClass(epicExplorer.epic)}`}>{epicExplorer.epic.compliance_label}</span>
        )}
      </div>

      <div className="epic-focus-grid landing-epic-focus-grid">
        <div className="table-wrap epic-issues-wrap">
          <div className="table-header">
            <p>Issues in epic</p>
          </div>
          <div className="epic-tree-caption" style={{ marginTop: 0 }}>
            Click any issue to expand its compliance rules, status findings, and field findings.
          </div>
          <div className="epic-issue-list">
            {epicIssueRows.length ? epicIssueRows.map((issue, index) => (
              <EpicIssueCard key={issue?.key || index} issue={issue} index={index} />
            )) : (
              <div className="empty epic-tree-empty">No issues were returned for this epic scope.</div>
            )}
          </div>
        </div>

        <div className="table-wrap">
          <div className="table-header"><p>Full hierarchy under the epic</p></div>
          <div className="epic-tree">
            {epicExplorer?.outcome?.key && (
              <div className="epic-tree-node outcome">
                <div className="epic-tree-node-header">
                  <span className="epic-type-pill outcome">Outcome</span>
                  <JiraIssueLink jiraBase={jiraBase} issueKey={epicExplorer.outcome.key}>{epicExplorer.outcome.key}</JiraIssueLink>
                  <span>{epicExplorer.outcome.summary}</span>
                  <span className="epic-node-status">{epicExplorer.outcome.status || 'Unknown'}</span>
                  <span className={`tree-rate ${getEpicIssueStateClass(epicExplorer.outcome)}`}>{getEpicIssueStateLabel(epicExplorer.outcome)}</span>
                </div>
                <div className="epic-tree-caption">{getEpicNodeIssueSummary(epicExplorer.outcome)}</div>
              </div>
            )}

            <div className="epic-tree-node epic">
              <div className="epic-tree-node-header">
                <span className="epic-type-pill epic">Epic</span>
                <JiraIssueLink jiraBase={jiraBase} issueKey={epicExplorer?.epic?.key}>{epicExplorer?.epic?.key}</JiraIssueLink>
                <span>{epicExplorer?.epic?.summary}</span>
                <span className="epic-node-status">{epicExplorer?.epic?.status || 'Unknown'}</span>
                <span className={`tree-rate ${getEpicIssueStateClass(epicExplorer?.epic)}`}>{getEpicIssueStateLabel(epicExplorer?.epic)}</span>
                <span className="tree-meta-chip">{epicExplorer?.issue_branches?.length || 0} branches</span>
              </div>
              <div className="epic-tree-caption">{getEpicNodeIssueSummary(epicExplorer?.epic)}</div>

              <div className="epic-tree-children">
                {epicExplorer?.issue_branches?.length ? epicExplorer.issue_branches.map(branch => (
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
  )
}
function HierarchyColumnHeader() {
  return (
    <div className="landing-hierarchy-columns-header" aria-hidden="true">
      <span>Level</span>
      <span>Ticket</span>
      <span>Title</span>
      <span>Assignee</span>
      <span>Status</span>
      <span>Issues in epic</span>
      <span>Status mismatch</span>
      <span>Field mismatch</span>
    </div>
  )
}
function HierarchyIssueModal({ issueModal, jiraBase, compactMode, onClose }) {
  const isStatus = issueModal.issueType === 'status'
  const isEpicIssues = issueModal.issueType === 'epic-issues'
  const compliantCount = isEpicIssues ? issueModal.details.filter(detail => detail.hasCompliance).length : 0
  const [epicFocusState, setEpicFocusState] = useState(() => ({
    loading: isEpicIssues,
    error: '',
    data: issueModal.epicFocus || null,
  }))

  useEffect(() => {
    let cancelled = false

    if (!isEpicIssues || !issueModal.nodeKey) {
      setEpicFocusState({ loading: false, error: '', data: null })
      return undefined
    }

    setEpicFocusState({ loading: true, error: '', data: issueModal.epicFocus || null })

    fetchJSON(`/api/hierarchy/epic-focus?epic_key=${encodeURIComponent(issueModal.nodeKey)}`)
      .then(result => {
        if (cancelled) return
        if (result?.error) {
          setEpicFocusState({ loading: false, error: result.error, data: null })
          return
        }
        setEpicFocusState({ loading: false, error: '', data: result })
      })
      .catch(error => {
        if (cancelled) return
        setEpicFocusState({ loading: false, error: `Epic hierarchy lookup failed: ${error.message}`, data: null })
      })

    return () => {
      cancelled = true
    }
  }, [isEpicIssues, issueModal.epicFocus, issueModal.nodeKey])

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={`modal-card landing-issue-modal ${compactMode ? 'compact-mode' : ''}`} onClick={event => event.stopPropagation()}>
        <div className="landing-panel-header landing-issue-modal-header">
          <div>
            <h2>{getIssueDialogLabel(issueModal.issueType)} details</h2>
            <span>
              {issueModal.nodeType} •{' '}
              <JiraIssueLink jiraBase={jiraBase} issueKey={issueModal.nodeKey}>{issueModal.nodeKey}</JiraIssueLink>
              {' '}• {issueModal.nodeStatus || 'Unknown status'}
            </span>
          </div>
          <button type="button" onClick={onClose}>Close</button>
        </div>
        {issueModal.nodeSummary && <p className="landing-section-intro landing-issue-modal-summary">{issueModal.nodeSummary}</p>}
        {isEpicIssues && (
          <div className="landing-issue-modal-summary landing-issue-modal-compliance">
            <strong>{compliantCount}</strong>
            <span>of</span>
            <strong>{issueModal.details.length}</strong>
            <span>issues are compliant on mandatory fields.</span>
          </div>
        )}
        {isEpicIssues && epicFocusState.loading && (
          <div className="dashboard-empty-note">Loading live epic hierarchy and compliance rules…</div>
        )}
        {isEpicIssues && epicFocusState.error && (
          <div className="landing-error-banner landing-epic-focus-error">{epicFocusState.error}</div>
        )}
        {isEpicIssues && epicFocusState.data ? (
          <div className="landing-issue-table-wrap landing-epic-focus-wrap">
            <EpicFocusDetails epicExplorer={epicFocusState.data} jiraBase={jiraBase} />
          </div>
        ) : issueModal.details.length === 0 ? (
          <div className="dashboard-empty-note">No detailed mismatch rows are available for this node yet.</div>
        ) : (
          <div className="landing-issue-table-wrap">
            {isStatus ? (
              <table className="landing-table landing-issue-table">
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th>Related issue</th>
                    <th>Related status</th>
                    <th>Source</th>
                    <th>Severity</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {issueModal.details.map((detail, index) => (
                    <tr key={`${detail.issueKey || 'issue'}-${detail.relatedKey || 'related'}-${index}`}>
                      <td><JiraIssueLink jiraBase={jiraBase} issueKey={detail.issueKey}>{detail.issueKey}</JiraIssueLink></td>
                      <td>{detail.issueType || 'Issue'}</td>
                      <td>{detail.issueStatus || '—'}</td>
                      <td><JiraIssueLink jiraBase={jiraBase} issueKey={detail.relatedKey}>{detail.relatedKey}</JiraIssueLink></td>
                      <td>{detail.relatedStatus || '—'}</td>
                      <td>{detail.source || getChildRelationLabel({ type: detail.issueType }, { type: detail.relatedType })}</td>
                      <td><span className={`severity-pill ${detail.severity || 'Low'}`}>{detail.severity || 'Low'}</span></td>
                      <td>{detail.reason || 'No mismatch reason captured.'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : isEpicIssues ? (
              <table className="landing-table landing-issue-table">
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th>Mandatory fields</th>
                    <th>Field compliance</th>
                    <th>Path</th>
                  </tr>
                </thead>
                <tbody>
                  {issueModal.details.map((detail, index) => (
                    <tr key={`${detail.issueKey || 'issue'}-${detail.hierarchyPath || 'path'}-${index}`}>
                      <td><JiraIssueLink jiraBase={jiraBase} issueKey={detail.issueKey}>{detail.issueKey}</JiraIssueLink></td>
                      <td>{detail.issueType || 'Issue'}</td>
                      <td>{detail.issueStatus || '—'}</td>
                      <td>
                        {detail.mandatoryFieldCount > 0 ? (
                          <>
                            <strong>{detail.mandatoryFieldCount}</strong>
                            <div className="muted-copy small">{(detail.missingFields || []).join(', ')}</div>
                          </>
                        ) : (
                          <span className="severity-pill Low">All mandatory fields met</span>
                        )}
                      </td>
                      <td>
                        <span className={`severity-pill ${detail.hasCompliance ? 'Low' : 'High'}`}>
                          {detail.fieldCompliance}
                        </span>
                      </td>
                      <td>{detail.hierarchyPath || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <table className="landing-table landing-issue-table">
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th>Field</th>
                    <th>Severity</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {issueModal.details.map((detail, index) => (
                    <tr key={`${detail.issueKey || 'issue'}-${detail.field || 'field'}-${index}`}>
                      <td><JiraIssueLink jiraBase={jiraBase} issueKey={detail.issueKey}>{detail.issueKey}</JiraIssueLink></td>
                      <td>{detail.issueType || issueModal.nodeType || 'Issue'}</td>
                      <td>{detail.issueStatus || '—'}</td>
                      <td>{detail.field || 'Unknown field'}</td>
                      <td><span className={`severity-pill ${detail.severity || 'Low'}`}>{detail.severity || 'Low'}</span></td>
                      <td>{detail.message || 'No field mismatch reason captured.'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
function HierarchyGroup({ group, jiraBase, onOpenIssueDetails }) {
  const [open, setOpen] = useState(true)
  return (
    <details open={open} onToggle={event => setOpen(event.currentTarget.open)} className="tree-label-group">
      <summary>
        <div className="tree-node-main tree-node-main--label">
          <div className="tree-node-content">
            <div className="tree-node-row tree-node-row--label">
              <div className="tree-node-cell">
                <span className="tree-relation-pill">Label</span>
              </div>
              <div className="tree-node-cell tree-node-cell--key">
                <strong>{group.label}</strong>
              </div>
              <div className="tree-node-cell tree-node-cell--title">
                <div className="tree-node-title">{group.summary}</div>
              </div>
              <div className="tree-node-cell">{getHierarchyAssignee(group)}</div>
              <div className="tree-node-cell">{getHierarchyMetaValue(group, ['status', 'state'])}</div>
              <div className="tree-node-cell">
                <MetricBadge
                  tone={group.statusMismatchCount > 0 ? 'status' : 'clear'}
                  label={getIssueBadgeLabel(group.statusMismatchCount, 'status')}
                  interactive={group.statusMismatchCount > 0}
                  onClick={event => onOpenIssueDetails?.(group, 'status', event)}
                  title={group.statusMismatchCount > 0 ? `Open status mismatches for ${group.label}` : undefined}
                />
              </div>
              <div className="tree-node-cell">
                <MetricBadge
                  tone={group.fieldMissingCount > 0 ? 'field' : 'clear'}
                  label={getIssueBadgeLabel(group.fieldMissingCount, 'field')}
                  interactive={group.fieldMissingCount > 0}
                  onClick={event => onOpenIssueDetails?.(group, 'field', event)}
                  title={group.fieldMissingCount > 0 ? `Open field mismatches for ${group.label}` : undefined}
                />
              </div>
            </div>
          </div>
          <div className="tree-node-metrics">
            <span className="tree-issues">{group.issueCount || 0} total</span>
            <span className={`tree-rate ${statusTone(group.complianceRate || 0)}`}>{group.complianceRate || 0}%</span>
          </div>
        </div>
      </summary>
      <div className="tree-node-children">
        {(group.children || []).map(outcome => (
          <HierarchyNode key={outcome.key} node={outcome} level={0} jiraBase={jiraBase} onOpenIssueDetails={onOpenIssueDetails} />
        ))}
      </div>
    </details>
  )
}
function HierarchyNode({ node, level, jiraBase, onOpenIssueDetails }) {
  const hasChildren = Array.isArray(node.children) && node.children.length > 0
  const [open, setOpen] = useState(level < 1)
  const tone = statusTone(node.complianceRate || 0)
  return (
    <details open={open} onToggle={event => setOpen(event.currentTarget.open)} className={`tree-node level-${level}`}>
      <summary>
        <div className="tree-node-main tree-node-main--compact">
          <div className="tree-node-content">
            <div className="tree-node-row">
              <div className="tree-node-cell">
                <span className="tree-relation-pill">{getHierarchyRelationLabel(node)}</span>
              </div>
              <div className="tree-node-cell tree-node-cell--key">
                <JiraIssueLink jiraBase={jiraBase} issueKey={node.key} onClick={event => event.stopPropagation()}>{node.key}</JiraIssueLink>
              </div>
              <div className="tree-node-cell tree-node-cell--title">
                <div className="tree-node-title">
                  <JiraIssueLink jiraBase={jiraBase} issueKey={node.key}>{node.summary || node.key}</JiraIssueLink>
                </div>
                {hasChildren && <div className="tree-node-summary">{node.children.length} child item{node.children.length === 1 ? '' : 's'}</div>}
              </div>
              <div className="tree-node-cell">{getHierarchyAssignee(node)}</div>
              <div className="tree-node-cell">{getHierarchyMetaValue(node, ['status', 'state'])}</div>
              <div className="tree-node-cell">
                <MetricBadge
                  tone={node.statusMismatchCount > 0 ? 'status' : 'clear'}
                  label={getIssueBadgeLabel(node.statusMismatchCount, 'status')}
                  interactive={node.statusMismatchCount > 0}
                  onClick={event => onOpenIssueDetails?.(node, 'status', event)}
                  title={node.statusMismatchCount > 0 ? `Open status mismatches for ${node.key}` : undefined}
                />
              </div>
              <div className="tree-node-cell">
                {node.type === 'Epic' ? (
                  <button
                    type="button"
                    className="tree-inline-link"
                    onClick={event => onOpenIssueDetails?.(node, 'epic-issues', event)}
                    title={`Open issues in epic for ${node.key}`}
                  >
                    {`Issues in epic (${collectEpicIssueRows(node).length})`}
                  </button>
                ) : (
                  <span className="tree-empty-cell">—</span>
                )}
              </div>
              <div className="tree-node-cell">
                <MetricBadge
                  tone={node.fieldMissingCount > 0 ? 'field' : 'clear'}
                  label={getIssueBadgeLabel(node.fieldMissingCount, 'field')}
                  interactive={node.fieldMissingCount > 0}
                  onClick={event => onOpenIssueDetails?.(node, 'field', event)}
                  title={node.fieldMissingCount > 0 ? `Open field mismatches for ${node.key}` : undefined}
                />
              </div>
            </div>
          </div>
          <div className="tree-node-metrics">
            <span className="tree-issues">{node.issueCount || 0} total</span>
            <span className={`tree-rate ${tone}`}>{node.complianceRate || 0}%</span>
          </div>
        </div>
      </summary>
      {hasChildren && (
        <div className="tree-node-children">
          {node.children.map(child => (
            <HierarchyNode key={child.key} node={child} level={level + 1} jiraBase={jiraBase} onOpenIssueDetails={onOpenIssueDetails} />
          ))}
        </div>
      )}
    </details>
  )
}
