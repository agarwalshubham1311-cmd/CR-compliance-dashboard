import React, { useState, useEffect, useCallback, useRef } from 'react'
import { BarChart, Bar, PieChart, Pie, Cell, XAxis, YAxis, ResponsiveContainer, Tooltip } from 'recharts'

const SEV_COLORS = { Critical: '#e34948', High: '#eda100', Medium: '#2a78d6', Low: '#898781' }

// Per-tab config: how to fetch, what columns to show, how each row's data is shaped.
const TABS = {
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
  outcome: {
    label: 'Outcome',
    columns: ['Story', 'Story status', 'Outcome', 'Outcome status', 'Reason', 'Severity'],
    getRows: d => ({ leftKey: d.cr_key, leftStatus: d.cr_status, rightKey: d.story_key, rightStatus: d.story_status, reason: d.reason, severity: d.severity, score: d.score }),
    hasActions: true, dependentSide: 'right',
  },
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts)
  return r.json()
}

export default function App() {
  const [currentTab, setCurrentTab] = useState('epic')
  const [jiraBase, setJiraBase] = useState('')
  const [data, setData] = useState({ epic: [], epicFields: [], cr: [], story: [], outcome: [], outcomeFields: [] })
  const [status, setStatus] = useState('')
  const [expandedKey, setExpandedKey] = useState(null)
  const [epicFieldsExpandedKey, setEpicFieldsExpandedKey] = useState(null)
  const [reasonFilter, setReasonFilter] = useState('all')
  const [sevFilter, setSevFilter] = useState('all')
  const [sortAsc, setSortAsc] = useState(false)
  const [draft, setDraft] = useState(null) // { forKey, text, statusMsg }
  const [statusChange, setStatusChange] = useState(null) // { forKey, transitions, selected, msg }
  const fieldCache = useRef({})

  const loadAll = useCallback(async () => {
    const [summary, epicPairs, outcomePairs, crFindings, epicFindings, outcomeFindings] = await Promise.all([
      fetchJSON('/api/dashboard/summary'),
      fetchJSON('/api/pairs/epic-cr'),
      fetchJSON('/api/pairs/story-outcome'),
      fetchJSON('/api/fields/findings?entity_type=cr'),
      fetchJSON('/api/fields/findings?entity_type=epic'),
      fetchJSON('/api/fields/findings?entity_type=outcome'),
    ])
    setData({
      story: summary.active || [],
      epic: Array.isArray(epicPairs) ? epicPairs : [],
      epicFields: Array.isArray(epicFindings) ? epicFindings : [],
      outcome: Array.isArray(outcomePairs) ? outcomePairs : [],
      cr: Array.isArray(crFindings) ? crFindings : [],
      outcomeFields: Array.isArray(outcomeFindings) ? outcomeFindings : [],
    })
    setStatus(summary.checked_at ? 'Last scan: ' + new Date(summary.checked_at * 1000).toLocaleString() : 'No scan yet')
  }, [])

  useEffect(() => {
    fetchJSON('/api/config').then(c => { if (c.jira_base) setJiraBase(c.jira_base) })
  }, [])

  useEffect(() => {
    loadAll()
    const id = setInterval(loadAll, 60000)
    return () => clearInterval(id)
  }, [loadAll])

  async function refresh() {
    setStatus('Scanning...')
    await fetchJSON('/api/dashboard/refresh', { method: 'POST' })
    loadAll()
  }

  async function resolveRow(storyKey, crKey) {
    await fetchJSON('/api/dashboard/resolve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ story_key: storyKey, cr_key: crKey }),
    })
    loadAll()
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
    setDraft(d => ({ ...d, statusMsg: result.error ? 'Error: ' + result.error : 'Posted \u2713' }))
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
      setStatusChange(s => ({ ...s, msg: 'Updated \u2713' }))
      setTimeout(() => { setStatusChange(null); loadAll() }, 800)
    }
  }

  function switchTab(tab) {
    setCurrentTab(tab)
    setExpandedKey(null)
    setReasonFilter('all')
    setSevFilter('all')
  }

  const cfg = TABS[currentTab]
  const epicFieldsCount = new Set([...data.epic.map(d => d.story_key), ...data.epicFields.map(f => f.entity_key)]).size
  const crCount = new Set(data.cr.map(f => f.entity_key)).size

  return (
    <div>
      <div className="toolbar">
        <div><span className="version-tag">React \u00b7 live data</span></div>
        <div>
          <span id="status">{status}</span>
          <button onClick={refresh}>Refresh now</button>
        </div>
      </div>
      <h2 style={{ margin: '0 0 16px' }}>Compliance dashboard</h2>

      <div className="tabs">
        {Object.entries(TABS).map(([key, t]) => (
          <button key={key} className={'tabbtn' + (currentTab === key ? ' active' : '')} onClick={() => switchTab(key)}>
            {t.label} <span className="count">{key === 'epic' ? epicFieldsCount : key === 'cr' ? crCount : data[key].length}</span>
          </button>
        ))}
      </div>

      <Cards tab={currentTab} data={data} />
      <Charts tab={currentTab} data={data} />

      <div className="filters">
        <ReasonFilter tab={currentTab} data={data} value={reasonFilter} onChange={setReasonFilter} />
        <select value={sevFilter} onChange={e => setSevFilter(e.target.value)}>
          <option value="all">All severities</option>
          <option>Critical</option><option>High</option><option>Medium</option><option>Low</option>
        </select>
      </div>

      {cfg.combined && (
        <PhaseTable
          cfg={cfg} rows={data.epic} reasonFilter={reasonFilter} sevFilter={sevFilter}
          sortAsc={sortAsc} setSortAsc={setSortAsc} expandedKey={expandedKey} setExpandedKey={setExpandedKey}
          onResolve={resolveRow} onDraft={draftPhaseComment} onStatusChange={openStatusBox}
          title="Phase mismatches" jiraBase={jiraBase}
        />
      )}
      {!cfg.combined && !cfg.fieldTab && (
        <PhaseTable
          cfg={cfg} rows={data[currentTab]} reasonFilter={reasonFilter} sevFilter={sevFilter}
          sortAsc={sortAsc} setSortAsc={setSortAsc} expandedKey={expandedKey} setExpandedKey={setExpandedKey}
          onResolve={resolveRow} onDraft={draftPhaseComment} onStatusChange={openStatusBox}
          title={'Non-compliant ' + currentTab + 's'} jiraBase={jiraBase}
        />
      )}
      {cfg.fieldTab && (
        <FieldTable
          findings={data.cr} reasonFilter={reasonFilter} sevFilter={sevFilter}
          entityType="cr" expandedKey={expandedKey} setExpandedKey={setExpandedKey}
          onDraft={draftFieldComment} fieldCache={fieldCache} onSaved={loadAll}
          title="Non-compliant CRs" jiraBase={jiraBase}
        />
      )}
      {cfg.combined && (
        <FieldTable
          findings={data.epicFields} reasonFilter="all" sevFilter="all"
          entityType="epic" expandedKey={epicFieldsExpandedKey} setExpandedKey={setEpicFieldsExpandedKey}
          onDraft={draftFieldComment} fieldCache={fieldCache} onSaved={loadAll}
          title="Field issues" jiraBase={jiraBase}
        />
      )}

      {draft && (
        <div id="draftBox">
          <p style={{ fontSize: 12, color: '#886', margin: '0 0 6px' }}>
            AI-drafted comment for <b>{draft.forKey}</b> \u2014 review before posting
          </p>
          <textarea value={draft.text} onChange={e => setDraft(d => ({ ...d, text: e.target.value }))} />
          <div className="actions">
            <button onClick={postDraft}>Post to Jira</button>
            <button onClick={() => setDraft(null)}>Discard</button>
          </div>
          <p id="draftStatus">{draft.statusMsg}</p>
        </div>
      )}

      {statusChange && (
        <div id="statusBox">
          <p style={{ fontSize: 12, color: '#185fa5', margin: '0 0 6px' }}>
            Change status for <b>{statusChange.forKey}</b>
          </p>
          {statusChange.transitions.length > 0 && (
            <select value={statusChange.selected} onChange={e => setStatusChange(s => ({ ...s, selected: e.target.value }))}>
              {statusChange.transitions.map(t => (
                <option key={t.id} value={t.id}>{t.to_status} ({t.name})</option>
              ))}
            </select>
          )}
          <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
            <button onClick={applyStatusChange}>Apply</button>
            <button onClick={() => setStatusChange(null)}>Cancel</button>
          </div>
          <p style={{ fontSize: 12, color: '#888', margin: '6px 0 0' }}>{statusChange.msg}</p>
        </div>
      )}
    </div>
  )
}

function ReasonFilter({ tab, data, value, onChange }) {
  const cfg = TABS[tab]
  const values = cfg.fieldTab
    ? [...new Set(data.cr.map(f => f.field).filter(Boolean))]
    : [...new Set((cfg.combined ? data.epic : data[tab]).map(cfg.getRows).map(r => r.reason).filter(Boolean))]
  return (
    <select value={value} onChange={e => onChange(e.target.value)}>
      <option value="all">All reasons</option>
      {values.map(v => <option key={v} value={v}>{v}</option>)}
    </select>
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

function PhaseTable({ cfg, rows, reasonFilter, sevFilter, sortAsc, setSortAsc, expandedKey, setExpandedKey, onResolve, onDraft, onStatusChange, title, jiraBase }) {
  let filtered = rows.map(cfg.getRows)
    .filter(r => (reasonFilter === 'all' || r.reason === reasonFilter) && (sevFilter === 'all' || r.severity === sevFilter))
    .sort((a, b) => sortAsc ? (a.score || 0) - (b.score || 0) : (b.score || 0) - (a.score || 0))

  const isTargetLeft = cfg.dependentSide !== 'right'

  return (
    <div className="table-wrap">
      <div className="table-header"><p>{title}</p></div>
      <table>
        <colgroup>{cfg.columns.map((_, i) => <col key={i} style={{ width: (cfg.hasActions ? [12, 16, 12, 16, 24, 12] : [14, 16, 14, 16, 26, 14])[i] + '%' }} />)}{cfg.hasActions && <col style={{ width: '8%' }} />}<col style={{ width: '4%' }} /></colgroup>
        <thead>
          <tr>
            {cfg.columns.map(c => c === 'Severity'
              ? <th key={c} style={{ cursor: 'pointer' }} onClick={() => setSortAsc(!sortAsc)}>Severity \u21c5</th>
              : <th key={c}>{c}</th>)}
            {cfg.hasActions && <th>Actions</th>}
            <th></th>
          </tr>
        </thead>
        <tbody>
          {filtered.length === 0 && (
            <tr><td colSpan={cfg.hasActions ? 7 : 6} className="empty">No mismatches match these filters.</td></tr>
          )}
          {filtered.map(d => {
            const targetKey = isTargetLeft ? d.leftKey : d.rightKey
            const targetStatus = isTargetLeft ? d.leftStatus : d.rightStatus
            const otherKey = isTargetLeft ? d.rightKey : d.leftKey
            const otherStatus = isTargetLeft ? d.rightStatus : d.leftStatus
            const isOpen = expandedKey === d.leftKey
            return (
              <React.Fragment key={d.leftKey + d.rightKey}>
                <tr className="data-row" onClick={() => setExpandedKey(isOpen ? null : d.leftKey)}>
                  <td><a className="key-link" href={jiraBase + d.leftKey} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>{d.leftKey}</a></td>
                  <td>{d.leftStatus}</td>
                  <td><a className="key-link" href={jiraBase + d.rightKey} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>{d.rightKey}</a></td>
                  <td>{d.rightStatus}</td>
                  <td><span className="badge badge-reason">{d.reason}</span></td>
                  <td><span className={'badge badge-' + d.severity}>{d.severity}</span></td>
                  {cfg.hasActions && (
                    <td onClick={e => e.stopPropagation()}>
                      {cfg.hasResolve && <button onClick={() => onResolve(d.leftKey, d.rightKey)}>Resolve</button>}{' '}
                      <button onClick={() => onStatusChange(targetKey)}>Change status</button>{' '}
                      <button onClick={() => onDraft(targetKey, targetStatus, otherKey, otherStatus, d.reason, d.severity)}>Draft</button>
                    </td>
                  )}
                  <td className="chevron">{isOpen ? '\u25be' : '\u25b8'}</td>
                </tr>
                {isOpen && (
                  <tr className="detail-row">
                    <td colSpan={cfg.hasActions ? 7 : 6}>
                      <div className="detail-inner">
                        <div className="detail-grid">
                          <div><span className="k">Reason</span><br />{d.reason}</div>
                          <div><span className="k">Severity score</span><br />{d.score || 0} pts</div>
                        </div>
                        <div className="detail-actions">
                          {cfg.hasActions && <>
                            <button onClick={() => onStatusChange(targetKey)}>Change status</button>
                            <button onClick={() => onDraft(targetKey, targetStatus, otherKey, otherStatus, d.reason, d.severity)}>Draft comment</button>
                          </>}
                          {cfg.hasResolve && <button onClick={() => onResolve(d.leftKey, d.rightKey)}>Mark resolved</button>}
                          <a href={jiraBase + d.leftKey} target="_blank" rel="noreferrer">Open in Jira \u2197</a>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function FieldTable({ findings, reasonFilter, sevFilter, entityType, expandedKey, setExpandedKey, onDraft, fieldCache, onSaved, title, jiraBase }) {
  const filtered = findings.filter(f => (reasonFilter === 'all' || f.field === reasonFilter) && (sevFilter === 'all' || f.severity === sevFilter))
  const rank = { Critical: 3, High: 2, Medium: 1, Low: 0 }
  const groups = {}
  filtered.forEach(f => {
    if (!groups[f.entity_key]) groups[f.entity_key] = { key: f.entity_key, status: f.entity_status, findings: [], maxRank: 0 }
    groups[f.entity_key].findings.push(f)
    groups[f.entity_key].maxRank = Math.max(groups[f.entity_key].maxRank, rank[f.severity] || 0)
  })
  const sevLabel = { 3: 'Critical', 2: 'High', 1: 'Medium', 0: 'Low' }
  const groupList = Object.values(groups).sort((a, b) => b.maxRank - a.maxRank)

  return (
    <div className="table-wrap">
      <div className="table-header"><p>{title}</p></div>
      <table>
        <colgroup><col style={{ width: '16%' }} /><col style={{ width: '18%' }} /><col style={{ width: '12%' }} /><col style={{ width: '26%' }} /><col style={{ width: '20%' }} /><col style={{ width: '8%' }} /></colgroup>
        <thead><tr><th>{entityType === 'epic' ? 'Epic' : 'CR'}</th><th>Status</th><th>Severity</th><th>Issues</th><th>Actions</th><th></th></tr></thead>
        <tbody>
          {groupList.length === 0 && <tr><td colSpan={6} className="empty">No findings.</td></tr>}
          {groupList.map(g => {
            const isOpen = expandedKey === g.key
            return (
              <React.Fragment key={g.key}>
                <tr className="data-row" onClick={() => setExpandedKey(isOpen ? null : g.key)}>
                  <td><a className="key-link" href={jiraBase + g.key} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>{g.key}</a></td>
                  <td>{g.status || '-'}</td>
                  <td><span className={'badge badge-' + sevLabel[g.maxRank]}>{sevLabel[g.maxRank]}</span></td>
                  <td>{g.findings.length} issue{g.findings.length > 1 ? 's' : ''}</td>
                  <td onClick={e => e.stopPropagation()}><button onClick={() => onDraft(g.key, g.status, g.findings)}>Draft</button></td>
                  <td className="chevron">{isOpen ? '\u25be' : '\u25b8'}</td>
                </tr>
                {isOpen && <EditableFieldsRow entityKey={g.key} entityType={entityType} fieldCache={fieldCache} onSaved={onSaved} />}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function EditableFieldsRow({ entityKey, entityType, fieldCache, onSaved }) {
  const [fields, setFields] = useState(null)
  const [error, setError] = useState(null)
  const [msgs, setMsgs] = useState({})

  useEffect(() => {
    let cancelled = false
    fetchJSON(`/api/entities/${entityType}/${entityKey}/fields`).then(result => {
      if (cancelled) return
      if (result.error) setError(result.error)
      else { setFields(result.fields); fieldCache.current[entityKey] = result.fields }
    })
    return () => { cancelled = true }
  }, [entityKey, entityType, fieldCache])

  async function save(fieldKey, value) {
    setMsgs(m => ({ ...m, [fieldKey]: 'Saving...' }))
    const result = await fetchJSON('/api/jira/update-field', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue_key: entityKey, entity_type: entityType, field_key: fieldKey, value }),
    })
    if (result.error) {
      setMsgs(m => ({ ...m, [fieldKey]: 'Error: ' + result.error }))
    } else {
      setMsgs(m => ({ ...m, [fieldKey]: 'Saved \u2713' }))
      setTimeout(onSaved, 1000)
    }
  }

  return (
    <tr className="detail-row">
      <td colSpan={6}>
        <div style={{ padding: '4px 20px 16px 44px' }}>
          {error && <div>Error loading fields: {error}</div>}
          {!error && !fields && <div>Loading current field values...</div>}
          {fields && fields.map(f => <FieldEditor key={f.key} field={f} msg={msgs[f.key]} onSave={v => save(f.key, v)} />)}
        </div>
      </td>
    </tr>
  )
}

function FieldEditor({ field, msg, onSave }) {
  const isSet = field.value !== null && field.value !== undefined && field.value !== ''
  const [val, setVal] = useState(
    field.type === 'labels' ? (Array.isArray(field.value) ? field.value.join(', ') : '') : (field.value ?? '')
  )

  let input
  if (field.type === 'select' && field.options) {
    input = (
      <select value={val} onChange={e => setVal(e.target.value)}>
        <option value="">Choose...</option>
        {field.options.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    )
  } else if (field.type === 'date') {
    input = <input type="date" value={val || ''} onChange={e => setVal(e.target.value)} />
  } else {
    input = <input type="text" value={val} onChange={e => setVal(e.target.value)} placeholder={field.type === 'labels' ? 'comma-separated' : ''} />
  }

  return (
    <div className="field-row">
      <span className="field-label">{field.label}</span>
      <span className={'field-state ' + (isSet ? 'set' : 'unset')}>{isSet ? 'set' : 'not set'}</span>
      {input}
      <button onClick={() => onSave(val)}>Save</button>
      <span style={{ fontSize: 12, color: '#888' }}>{msg}</span>
    </div>
  )
}
