import React, { useState, useEffect } from "react";
import "./styles.css";

export default function App() {
  const [activeTab, setActiveTab] = useState("cr-story");
  const [summary, setSummary] = useState({ total_stories: 0, non_compliant_stories: 0, active: [] });
  const [epicCrFindings, setEpicCrFindings] = useState([]);
  const [fieldFindings, setFieldFindings] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalData, setModalData] = useState(null);
  const [draftComment, setDraftComment] = useState("");
  const [actionLoading, setActionLoading] = useState(false);

  const fetchData = async () => {
    setLoading(true);
    try {
      const [sumRes, epicRes, fieldRes] = await Promise.all([
        fetch("/api/dashboard/summary").then((r) => r.json()),
        fetch("/api/pairs/epic-cr").then((r) => r.json()),
        fetch("/api/fields/findings").then((r) => r.json())
      ]);
      setSummary(sumRes || { total_stories: 0, non_compliant_stories: 0, active: [] });
      setEpicCrFindings(Array.isArray(epicRes) ? epicRes : []);
      setFieldFindings(Array.isArray(fieldRes) ? fieldRes : []);
    } catch (err) {
      console.error("Failed to load dashboard data:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const handleTriggerRefresh = async () => {
    setLoading(true);
    try {
      await fetch("/api/dashboard/refresh", { method: "POST" });
      await fetchData();
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  const openAiRemediation = async (row) => {
    setActionLoading(true);
    setModalData(row);
    setDraftComment("Generating AI suggestion...");
    try {
      const res = await fetch("/api/ai/draft-comment", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: "phase",
          target_key: row.story_key,
          target_status: row.story_status,
          other_key: row.cr_key,
          other_status: row.cr_status,
          reason: row.reason,
          severity: row.severity
        })
      });
      const data = await res.json();
      setDraftComment(data.draft || "Unable to draft remediation comment.");
    } catch (err) {
      setDraftComment("Failed to contact LLM backend.");
    } finally {
      setActionLoading(false);
    }
  };

  const handlePostComment = async () => {
    if (!modalData || !draftComment) return;
    setActionLoading(true);
    try {
      await fetch("/api/ai/post-comment", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          story_key: modalData.story_key || modalData.entity_key,
          comment: draftComment
        })
      });
      setModalData(null);
    } catch (err) {
      alert("Failed to post comment to Jira.");
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <div className="dashboard-container">
      {/* Header */}
      <header className="dashboard-header">
        <div className="header-title">
          <h1>Workflow Compliance Center</h1>
          <p>Live MCP Jira alignment, bottleneck detection, and auto-remediation</p>
        </div>
        <div className="header-actions">
          <button className="btn btn-outline" onClick={fetchData} disabled={loading}>
            Fetch Cached
          </button>
          <button className="btn btn-primary" onClick={handleTriggerRefresh} disabled={loading}>
            {loading ? "Scanning..." : "Trigger Full Scan"}
          </button>
        </div>
      </header>

      {/* Metrics Row */}
      <section className="metrics-grid">
        <div className="metric-card">
          <span className="metric-label">Active Discrepancies</span>
          <span className="metric-value">{summary.active ? summary.active.length : 0}</span>
          <span className="metric-sub">CR ⟷ Story Mismatches</span>
        </div>
        <div className="metric-card">
          <span className="metric-label">Epic Blockers</span>
          <span className="metric-value">{epicCrFindings.length}</span>
          <span className="metric-sub">Bottlenecked Epics</span>
        </div>
        <div className="metric-card">
          <span className="metric-label">Field Violations</span>
          <span className="metric-value">{fieldFindings.length}</span>
          <span className="metric-sub">Missing required Jira fields</span>
        </div>
      </section>

      {/* Tabs */}
      <div className="tab-bar">
        <button
          className={`tab-btn ${activeTab === "cr-story" ? "active" : ""}`}
          onClick={() => setActiveTab("cr-story")}
        >
          CR vs. Story Pairs ({summary.active ? summary.active.length : 0})
        </button>
        <button
          className={`tab-btn ${activeTab === "epic-cr" ? "active" : ""}`}
          onClick={() => setActiveTab("epic-cr")}
        >
          Epic Bottlenecks ({epicCrFindings.length})
        </button>
        <button
          className={`tab-btn ${activeTab === "fields" ? "active" : ""}`}
          onClick={() => setActiveTab("fields")}
        >
          Field Findings ({fieldFindings.length})
        </button>
      </div>

      {/* Active Tab View */}
      <div className="table-wrapper">
        <div className="table-container">
          {activeTab === "cr-story" && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Story</th>
                  <th>Story Status</th>
                  <th>CR Link</th>
                  <th>CR Status</th>
                  <th>Severity</th>
                  <th>Reason</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {summary.active && summary.active.length > 0 ? (
                  summary.active.map((row, idx) => (
                    <tr key={idx}>
                      <td><strong>{row.story_key}</strong></td>
                      <td>{row.story_status}</td>
                      <td><strong>{row.cr_key}</strong></td>
                      <td>{row.cr_status}</td>
                      <td>
                        <span className={`badge badge-${(row.severity || "low").toLowerCase()}`}>
                          {row.severity || "INFO"}
                        </span>
                      </td>
                      <td style={{ color: "var(--text-muted)" }}>{row.reason}</td>
                      <td>
                        <button className="btn btn-outline btn-sm" onClick={() => openAiRemediation(row)}>
                          AI Fix
                        </button>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr><td colSpan="7" style={{ textAlign: "center", padding: "2rem" }}>No active CR/Story mismatches found.</td></tr>
                )}
              </tbody>
            </table>
          )}

          {activeTab === "epic-cr" && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Epic Key</th>
                  <th>Epic Status</th>
                  <th>Bottleneck CR</th>
                  <th>CR Status</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {epicCrFindings.length > 0 ? (
                  epicCrFindings.map((row, idx) => (
                    <tr key={idx}>
                      <td><strong>{row.story_key}</strong></td>
                      <td>{row.story_status}</td>
                      <td><strong>{row.cr_key}</strong></td>
                      <td>{row.cr_status}</td>
                      <td>{row.reason}</td>
                    </tr>
                  ))
                ) : (
                  <tr><td colSpan="5" style={{ textAlign: "center", padding: "2rem" }}>No Epic bottlenecks detected.</td></tr>
                )}
              </tbody>
            </table>
          )}

          {activeTab === "fields" && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Entity</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Missing / Invalid Fields</th>
                </tr>
              </thead>
              <tbody>
                {fieldFindings.length > 0 ? (
                  fieldFindings.map((row, idx) => (
                    <tr key={idx}>
                      <td><strong>{row.entity_key}</strong></td>
                      <td><span className="badge badge-low">{row.entity_type}</span></td>
                      <td>{row.entity_status}</td>
                      <td style={{ color: "var(--danger)" }}>
                        {Array.isArray(row.findings) ? row.findings.join(", ") : JSON.stringify(row.findings)}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr><td colSpan="4" style={{ textAlign: "center", padding: "2rem" }}>No field validation issues found.</td></tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* AI Remediation Modal */}
      {modalData && (
        <div className="modal-overlay">
          <div className="modal-content">
            <h3>Draft Remediation Comment</h3>
            <p style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
              Issue: <strong>{modalData.story_key}</strong> | Conflicting with <strong>{modalData.cr_key}</strong>
            </p>
            <textarea
              value={draftComment}
              onChange={(e) => setDraftComment(e.target.value)}
              disabled={actionLoading}
            />
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem" }}>
              <button className="btn btn-outline" onClick={() => setModalData(null)}>Cancel</button>
              <button className="btn btn-primary" onClick={handlePostComment} disabled={actionLoading}>
                {actionLoading ? "Submitting..." : "Post to Jira"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}