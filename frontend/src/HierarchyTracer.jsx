import React, { useState, useEffect } from 'react'
import './HierarchyTracer.css'

export default function HierarchyTracer() {
  const [labels, setLabels] = useState([])
  const [selectedLabel, setSelectedLabel] = useState(null)
  const [hierarchy, setHierarchy] = useState(null)
  const [complianceIssues, setComplianceIssues] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [expandedNodes, setExpandedNodes] = useState({})
  const [severityFilter, setSeverityFilter] = useState('all')
  const [viewMode, setViewMode] = useState('hierarchy') // 'hierarchy' or 'issues'
  const [queryTime, setQueryTime] = useState(null)
  const [issueModal, setIssueModal] = useState(null)

  useEffect(() => {
    loadLabels()
  }, [])

  useEffect(() => {
    if (!issueModal) return undefined

    function handleEscape(event) {
      if (event.key === 'Escape') {
        setIssueModal(null)
      }
    }

    window.addEventListener('keydown', handleEscape)
    return () => window.removeEventListener('keydown', handleEscape)
  }, [issueModal])

  async function loadLabels() {
    setLoading(true)
    setError(null)
    try {
      const response = await fetch('/api/hierarchy/labels')
      const data = await response.json()
      if (data.error) {
        setError(data.error)
      } else {
        setLabels(data.labels || [])
        setQueryTime(data.query_time_ms)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function loadHierarchy(label = null) {
    setLoading(true)
    setError(null)
    try {
      let url = '/api/hierarchy/trace?quick=true'
      if (label) url += `&label=${encodeURIComponent(label)}`

      const response = await fetch(url)
      const data = await response.json()
      if (data.error) {
        setError(data.error)
      } else {
        setHierarchy(data)
        setQueryTime(data.summary?.query_time_ms)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function loadComplianceIssues() {
    setLoading(true)
    setError(null)
    try {
      let url = '/api/hierarchy/compliance-issues'
      if (severityFilter !== 'all') url += `?severity=${severityFilter}`

      const response = await fetch(url)
      const data = await response.json()
      if (data.error) {
        setError(data.error)
      } else {
        setComplianceIssues(data)
        setQueryTime(data.query_time_ms)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  function toggleNode(nodeKey) {
    setExpandedNodes(prev => ({
      ...prev,
      [nodeKey]: !prev[nodeKey]
    }))
  }

  function handleLabelSelect(label) {
    setSelectedLabel(label)
    loadHierarchy(label)
    setViewMode('hierarchy')
  }

  function handleViewComplianceIssues() {
    loadComplianceIssues()
    setViewMode('issues')
  }

  function closeIssueModal() {
    setIssueModal(null)
  }

  function getIssueBadgeLabel(count, type, compact = false) {
    if (type === 'status') {
      return compact ? `${count} status` : `${count} status mismatch${count !== 1 ? 'es' : ''}`
    }

    return compact ? `${count} field${count !== 1 ? 's' : ''}` : `${count} field issue${count !== 1 ? 's' : ''}`
  }

  function getStatusDetails(node, details) {
    if (details?.status_mismatches?.length > 0) {
      return details.status_mismatches.map(issue => ({
        relatedKey: issue.key || issue.related_key,
        relatedStatus: issue.status || issue.related_status,
        reason: issue.reason,
        severity: issue.severity,
      }))
    }

    return (node.statusIssues || []).map(issue => ({
      relatedKey: issue.story_key || issue.related_key,
      relatedStatus: issue.story_status || issue.related_status,
      reason: issue.reason,
      severity: issue.severity,
    }))
  }

  function getFieldDetails(node, details) {
    if (details?.field_issues?.length > 0) {
      return details.field_issues.map(issue => ({
        field: issue.field,
        message: issue.message,
        severity: issue.severity,
      }))
    }

    return (node.fieldIssues || []).map(issue => ({
      field: issue.field,
      message: issue.message || issue.reason,
      severity: issue.severity,
    }))
  }

  async function openIssueDetails(node, issueType, event) {
    event.stopPropagation()

    const issueCount = issueType === 'status'
      ? (node.statusIssues?.length || 0)
      : (node.fieldIssues?.length || 0)

    if (!issueCount) return

    const requestId = `${node.key}-${issueType}-${Date.now()}`

    setIssueModal({
      id: requestId,
      issueType,
      issueCount,
      nodeKey: node.key,
      nodeType: node.type,
      nodeStatus: node.status,
      summary: node.summary,
      description: '',
      details: [],
      loading: true,
      error: null,
    })

    try {
      const response = await fetch(`/api/hierarchy/drill-down?key=${encodeURIComponent(node.key)}`)
      const data = await response.json()

      if (data.error) {
        setIssueModal(current => current?.id === requestId
          ? { ...current, loading: false, error: data.error }
          : current)
        return
      }

      const normalizedDetails = issueType === 'status'
        ? getStatusDetails(node, data)
        : getFieldDetails(node, data)

      setIssueModal(current => current?.id === requestId
        ? {
            ...current,
            loading: false,
            summary: data.summary || current.summary,
            description: data.description || '',
            nodeStatus: data.status || current.nodeStatus,
            details: normalizedDetails,
          }
        : current)
    } catch (err) {
      setIssueModal(current => current?.id === requestId
        ? { ...current, loading: false, error: err.message }
        : current)
    }
  }

  const IssueBadge = ({ count, type, node, compact = false }) => {
    if (!count) return null

    return (
      <button
        type="button"
        className={`issue-badge-button ${type}-issues-badge`}
        onClick={event => openIssueDetails(node, type, event)}
        title={`View ${type} issue details for ${node.key}`}
      >
        {getIssueBadgeLabel(count, type, compact)}
      </button>
    )
  }

  const HierarchyNode = ({ node, level = 0 }) => {
    const isExpanded = expandedNodes[node.key]
    const hasChildren = node.epics?.length > 0 || node.crs?.length > 0 || node.stories?.length > 0

    return (
      <div className={`hierarchy-node level-${level}`} key={node.key}>
        <div className="node-header" onClick={() => toggleNode(node.key)}>
          {hasChildren && (
            <span className="expander">{isExpanded ? '▼' : '▶'}</span>
          )}
          {!hasChildren && <span className="expander-placeholder"></span>}

          <span className={`node-type badge badge-${node.type.toLowerCase()}`}>
            {node.type}
          </span>

          <span className="node-key">{node.key}</span>

          <span className="node-status" title={node.status}>
            {node.status}
          </span>

          <IssueBadge count={node.statusIssues?.length} type="status" node={node} />
          <IssueBadge count={node.fieldIssues?.length} type="field" node={node} />
        </div>

        {isExpanded && node.epics && node.epics.length > 0 && (
          <div className="node-children">
            {node.epics.map(epic => (
              <EpicNode key={epic.key} epic={epic} level={level + 1} />
            ))}
          </div>
        )}

        {isExpanded && node.crs && node.crs.length > 0 && (
          <div className="node-children">
            {node.crs.map(cr => (
              <CRNode key={cr.key} cr={cr} level={level + 1} />
            ))}
          </div>
        )}

        {isExpanded && node.stories && node.stories.length > 0 && (
          <div className="node-children">
            {node.stories.map(story => (
              <StoryNode key={story.key} story={story} level={level + 1} />
            ))}
          </div>
        )}
      </div>
    )
  }

  const EpicNode = ({ epic, level }) => {
    const isExpanded = expandedNodes[epic.key]
    const hasChildren = epic.crs?.length > 0

    return (
      <div className={`hierarchy-node level-${level} epic-node`} key={epic.key}>
        <div className="node-header" onClick={() => toggleNode(epic.key)}>
          {hasChildren && (
            <span className="expander">{isExpanded ? '▼' : '▶'}</span>
          )}
          {!hasChildren && <span className="expander-placeholder"></span>}

          <span className="node-type badge badge-epic">Epic</span>
          <span className="node-key">{epic.key}</span>
          <span className="node-status">{epic.status}</span>

          <IssueBadge count={epic.statusIssues?.length} type="status" node={epic} compact />
          {epic.hierarchyIssues?.length > 0 && (
            <span className="hierarchy-issues-badge">{epic.hierarchyIssues.length} hierarchy issue{epic.hierarchyIssues.length !== 1 ? 's' : ''}</span>
          )}
        </div>

        {isExpanded && epic.crs && epic.crs.length > 0 && (
          <div className="node-children">
            {epic.crs.map(cr => (
              <CRNode key={cr.key} cr={cr} level={level + 1} />
            ))}
          </div>
        )}
      </div>
    )
  }

  const CRNode = ({ cr, level }) => {
    const isExpanded = expandedNodes[cr.key]
    const hasChildren = cr.stories?.length > 0

    return (
      <div className={`hierarchy-node level-${level} cr-node`} key={cr.key}>
        <div className="node-header" onClick={() => toggleNode(cr.key)}>
          {hasChildren && (
            <span className="expander">{isExpanded ? '▼' : '▶'}</span>
          )}
          {!hasChildren && <span className="expander-placeholder"></span>}

          <span className="node-type badge badge-cr">CR</span>
          <span className="node-key">{cr.key}</span>
          <span className="node-status">{cr.status}</span>

          <IssueBadge count={cr.statusIssues?.length} type="status" node={cr} compact />
          <IssueBadge count={cr.fieldIssues?.length} type="field" node={cr} compact />
        </div>

        {isExpanded && cr.stories && cr.stories.length > 0 && (
          <div className="node-children">
            {cr.stories.map(story => (
              <StoryNode key={story.key} story={story} level={level + 1} />
            ))}
          </div>
        )}
      </div>
    )
  }

  const StoryNode = ({ story, level }) => {
    return (
      <div className={`hierarchy-node level-${level} story-node`} key={story.key}>
        <div className="node-header">
          <span className="expander-placeholder"></span>
          <span className="node-type badge badge-story">Story</span>
          <span className="node-key">{story.key}</span>
          <span className="node-status">{story.status}</span>
          {story.compliant === false && <span className="badge badge-noncompliant">Non-compliant</span>}
        </div>
      </div>
    )
  }

  return (
    <div className="hierarchy-tracer">
      <div className="ht-header">
        <h1>Hierarchy Traceability & Compliance</h1>
        <p>Fast hierarchy traversal with status mismatches and field gaps</p>
      </div>

      {error && (
        <div className="error-banner">
          <strong>Error:</strong> {error}
        </div>
      )}

      <div className="ht-controls">
        <div className="control-group">
          <label>View Mode</label>
          <div className="button-group">
            <button
              className={viewMode === 'hierarchy' ? 'active' : ''}
              onClick={() => {
                setViewMode('hierarchy')
                if (!hierarchy && labels.length > 0) {
                  loadHierarchy(selectedLabel)
                }
              }}
            >
              Hierarchy View
            </button>
            <button
              className={viewMode === 'issues' ? 'active' : ''}
              onClick={handleViewComplianceIssues}
            >
              Compliance Issues
            </button>
          </div>
        </div>

        {viewMode === 'hierarchy' && (
          <div className="control-group">
            <label>Select Label</label>
            <div className="label-selector">
              <button
                className={selectedLabel === null ? 'selected' : ''}
                onClick={() => {
                  setSelectedLabel(null)
                  loadHierarchy(null)
                }}
              >
                All Labels
              </button>
              {labels.map(label => (
                <button
                  key={label.name}
                  className={selectedLabel === label.name ? 'selected' : ''}
                  onClick={() => handleLabelSelect(label.name)}
                  title={`${label.outcome_count} outcomes, ${label.epic_count} epics`}
                >
                  {label.name}
                  <span className="count">{label.outcome_count}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {viewMode === 'issues' && (
          <div className="control-group">
            <label>Severity Filter</label>
            <select value={severityFilter} onChange={e => setSeverityFilter(e.target.value)}>
              <option value="all">All Severities</option>
              <option value="Critical">Critical</option>
              <option value="High">High</option>
              <option value="Medium">Medium</option>
              <option value="Low">Low</option>
            </select>
          </div>
        )}

        {queryTime !== null && (
          <div className="query-time">
            Query time: <strong>{queryTime}ms</strong>
          </div>
        )}
      </div>

      {loading && <div className="loading">Loading...</div>}

      {!loading && viewMode === 'hierarchy' && hierarchy && (
        <div className="ht-content">
          <div className="ht-summary">
            <div className="summary-stat">
              <span className="label">Outcomes</span>
              <span className="value">{hierarchy.summary.total_outcomes}</span>
            </div>
            <div className="summary-stat">
              <span className="label">Epics</span>
              <span className="value">{hierarchy.summary.total_epics}</span>
            </div>
            <div className="summary-stat">
              <span className="label">CRs</span>
              <span className="value">{hierarchy.summary.total_crs}</span>
            </div>
            <div className="summary-stat">
              <span className="label">Stories</span>
              <span className="value">{hierarchy.summary.total_stories}</span>
            </div>
            <div className="summary-stat alert">
              <span className="label">Status Mismatches</span>
              <span className="value">{hierarchy.summary.status_mismatches}</span>
            </div>
            <div className="summary-stat alert">
              <span className="label">Field Gaps</span>
              <span className="value">{hierarchy.summary.field_gaps}</span>
            </div>
          </div>

          <div className="hierarchy-tree">
            {hierarchy.outcomes.length === 0 ? (
              <div className="empty-state">No outcomes found for this label.</div>
            ) : (
              hierarchy.outcomes.map(outcome => (
                <HierarchyNode key={outcome.key} node={outcome} level={0} />
              ))
            )}
          </div>
        </div>
      )}

      {!loading && viewMode === 'issues' && complianceIssues && (
        <div className="ht-issues">
          <div className="issues-summary">
            <div className="stat">
              <span className="label">Status Mismatches</span>
              <span className="value">{complianceIssues.status_mismatches.length}</span>
            </div>
            <div className="stat">
              <span className="label">Field Gaps</span>
              <span className="value">{complianceIssues.field_gaps.length}</span>
            </div>
            <div className="stat">
              <span className="label">Total Issues</span>
              <span className="value">{complianceIssues.total_issues}</span>
            </div>
          </div>

          {complianceIssues.status_mismatches.length > 0 && (
            <div className="issues-section">
              <h3>Status Mismatches</h3>
              <table className="issues-table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th>Related</th>
                    <th>Reason</th>
                    <th>Severity</th>
                  </tr>
                </thead>
                <tbody>
                  {complianceIssues.status_mismatches.map((issue, i) => (
                    <tr key={i}>
                      <td><a href={`/browse/${issue.key}`} target="_blank" rel="noreferrer">{issue.key}</a></td>
                      <td>{issue.type}</td>
                      <td>{issue.status}</td>
                      <td><a href={`/browse/${issue.related_key}`} target="_blank" rel="noreferrer">{issue.related_key}</a></td>
                      <td>{issue.reason}</td>
                      <td><span className={`badge badge-${issue.severity.toLowerCase()}`}>{issue.severity}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {complianceIssues.field_gaps.length > 0 && (
            <div className="issues-section">
              <h3>Field Gaps</h3>
              <table className="issues-table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th>Field</th>
                    <th>Message</th>
                    <th>Severity</th>
                  </tr>
                </thead>
                <tbody>
                  {complianceIssues.field_gaps.map((issue, i) => (
                    <tr key={i}>
                      <td><a href={`/browse/${issue.key}`} target="_blank" rel="noreferrer">{issue.key}</a></td>
                      <td>{issue.type}</td>
                      <td>{issue.status}</td>
                      <td>{issue.field}</td>
                      <td>{issue.message}</td>
                      <td><span className={`badge badge-${issue.severity.toLowerCase()}`}>{issue.severity}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {complianceIssues.total_issues === 0 && (
            <div className="empty-state">No compliance issues found!</div>
          )}
        </div>
      )}

      {issueModal && (
        <div className="modal-overlay" onClick={closeIssueModal}>
          <div className="modal-card hierarchy-issue-modal" onClick={event => event.stopPropagation()}>
            <div className="modal-header hierarchy-issue-modal-header">
              <div>
                <p className="hierarchy-issue-modal-title">
                  {getIssueBadgeLabel(issueModal.issueCount, issueModal.issueType)} details
                </p>
                <p className="hierarchy-issue-modal-subtitle">
                  <span className="node-key">{issueModal.nodeKey}</span>
                  <span>{issueModal.nodeType}</span>
                  <span>{issueModal.nodeStatus || 'Unknown status'}</span>
                </p>
              </div>
              <button className="modal-close" onClick={closeIssueModal} aria-label="Close">×</button>
            </div>

            <div className="hierarchy-issue-modal-body">
              {issueModal.summary && (
                <div className="hierarchy-issue-summary">
                  <strong>Summary:</strong> {issueModal.summary}
                </div>
              )}

              {issueModal.loading && <div className="loading">Loading mismatch details...</div>}

              {!issueModal.loading && issueModal.error && (
                <div className="error-banner">
                  <strong>Error:</strong> {issueModal.error}
                </div>
              )}

              {!issueModal.loading && !issueModal.error && issueModal.details.length === 0 && (
                <div className="empty-state">No detailed mismatch information is available for this item.</div>
              )}

              {!issueModal.loading && !issueModal.error && issueModal.details.length > 0 && issueModal.issueType === 'status' && (
                <div className="hierarchy-issue-details-list">
                  {issueModal.details.map((issue, index) => (
                    <div className="hierarchy-issue-detail-card" key={`${issue.relatedKey || 'status'}-${index}`}>
                      <div className="hierarchy-issue-detail-topline">
                        <span className="hierarchy-issue-detail-label">Related issue</span>
                        <a href={`/browse/${issue.relatedKey}`} target="_blank" rel="noreferrer" className="hierarchy-issue-link">
                          {issue.relatedKey || 'N/A'}
                        </a>
                        <span className="node-status hierarchy-modal-status">{issue.relatedStatus || 'Unknown status'}</span>
                        <span className={`badge badge-${(issue.severity || 'Medium').toLowerCase()}`}>{issue.severity || 'Medium'}</span>
                      </div>
                      <div className="hierarchy-issue-detail-reason">
                        <span className="hierarchy-issue-detail-label">Why mismatched</span>
                        <p>{issue.reason || 'Reason not available.'}</p>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {!issueModal.loading && !issueModal.error && issueModal.details.length > 0 && issueModal.issueType === 'field' && (
                <div className="hierarchy-issue-details-list">
                  {issueModal.details.map((issue, index) => (
                    <div className="hierarchy-issue-detail-card" key={`${issue.field || 'field'}-${index}`}>
                      <div className="hierarchy-issue-detail-topline">
                        <span className="hierarchy-issue-detail-label">Field</span>
                        <strong>{issue.field || 'Unknown field'}</strong>
                        <span className={`badge badge-${(issue.severity || 'Medium').toLowerCase()}`}>{issue.severity || 'Medium'}</span>
                      </div>
                      <div className="hierarchy-issue-detail-reason">
                        <span className="hierarchy-issue-detail-label">Reason</span>
                        <p>{issue.message || 'No field mismatch reason available.'}</p>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {!issueModal.loading && !issueModal.error && issueModal.description && (
                <div className="hierarchy-issue-description">
                  <span className="hierarchy-issue-detail-label">Description</span>
                  <p>{issueModal.description}</p>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

