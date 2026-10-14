import React, { useEffect, useMemo, useState } from 'react'
import './LoginPage.css'

const JIRA_TYPES = {
  cloud: {
    label: 'Jira Cloud',
    helper: 'Use your Atlassian email address and API token.',
    usernameLabel: 'Email Address',
    usernameType: 'email',
    usernamePlaceholder: 'your-email@company.com',
    secretLabel: 'API Token',
    secretPlaceholder: 'Atlassian API token',
    secretHelp: 'Generate a token at https://id.atlassian.com/manage/api-tokens',
  },
  datacenter: {
    label: 'Jira Data Center',
    helper: 'Use your Jira username and password.',
    usernameLabel: 'Username',
    usernameType: 'text',
    usernamePlaceholder: 'jira_username',
    secretLabel: 'Password',
    secretPlaceholder: 'Your Jira password',
    secretHelp: 'Use the password for your self-hosted Jira instance.',
  },
}

export default function LoginPage({ onLoginSuccess }) {
  const [jiraUrl, setJiraUrl] = useState('')
  const [jiraType, setJiraType] = useState('cloud')
  const [username, setUsername] = useState('')
  const [passwordOrToken, setPasswordOrToken] = useState('')
  const [loading, setLoading] = useState(false)
  const [testingConnection, setTestingConnection] = useState(false)
  const [error, setError] = useState('')
  const [messages, setMessages] = useState([])
  const [showGuidance, setShowGuidance] = useState(false)
  const [guidance, setGuidance] = useState('')

  const typeConfig = useMemo(() => JIRA_TYPES[jiraType] || JIRA_TYPES.cloud, [jiraType])

  useEffect(() => {
    setShowGuidance(false)
    setGuidance('')
  }, [jiraType])

  const loadGuidance = async () => {
    try {
      const response = await fetch('/api/jira/guidance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jira_type: jiraType,
          question: `How do I log in to ${typeConfig.label}?`,
        }),
      })
      const result = await response.json()
      setGuidance(result.guidance || '')
    } catch (err) {
      console.error('Error loading guidance:', err)
    }
  }

  const validateAndConnect = async (event) => {
    event.preventDefault()
    setLoading(true)
    setError('')
    setMessages(['🔍 Checking Jira URL...'])

    try {
      const urlResponse = await fetch('/api/jira/validate-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jira_url: jiraUrl }),
      })
      const urlResult = await urlResponse.json()
      setMessages(prev => [...prev, urlResult.message || urlResult.error || 'URL check complete'])

      if (!urlResult.valid) {
        setError(urlResult.error || 'Invalid Jira URL')
        return
      }

      if (!username || !passwordOrToken) {
        setError('Please enter both username/email and password/API token.')
        return
      }

      setMessages(prev => [...prev, `🚀 Connecting to ${typeConfig.label}...`])

      const loginResponse = await fetch('/api/jira/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jira_url: jiraUrl,
          username,
          password_or_token: passwordOrToken,
          jira_type: jiraType,
        }),
      })

      const loginResult = await loginResponse.json()
      setMessages(loginResult.messages || [])

      if (loginResult.success) {
        setMessages(prev => [...prev, '✅ Login successful'])
        setTimeout(() => onLoginSuccess(loginResult), 700)
      } else {
        setError(loginResult.error || 'Login failed')
      }
    } catch (err) {
      setError(`Connection error: ${err.message}`)
      setMessages(prev => [...prev, `❌ Connection error: ${err.message}`])
    } finally {
      setLoading(false)
    }
  }

  const testConnection = async () => {
    setTestingConnection(true)
    setError('')
    setMessages(['🔎 Testing Jira connection...'])

    try {
      const response = await fetch('/api/jira/test-connection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jira_url: jiraUrl,
          username,
          password_or_token: passwordOrToken,
          jira_type: jiraType,
        }),
      })

      const result = await response.json()
      setMessages(result.messages || [])

      if (result.success) {
        setMessages(prev => [...prev, '✅ Connection test successful'])
      } else {
        setError(result.error || 'Connection test failed')
      }
    } catch (err) {
      setError(`Connection test error: ${err.message}`)
      setMessages(prev => [...prev, `❌ Connection test error: ${err.message}`])
    } finally {
      setTestingConnection(false)
    }
  }

  return (
    <div className="login-container">
      <div className="login-card">
        <div className="login-header">
          <span className="login-eyebrow">Secure Jira access</span>
          <h1>Connect your Jira workspace</h1>
          <p>Sign in with Jira Cloud or Data Center credentials to open the compliance dashboard.</p>
        </div>

        <form className="login-form" onSubmit={validateAndConnect}>
          <div className="form-group">
            <label htmlFor="jira-url">Jira URL</label>
            <input
              id="jira-url"
              type="url"
              placeholder="https://yourcompany.atlassian.net or https://jira.internal"
              value={jiraUrl}
              onChange={(e) => setJiraUrl(e.target.value)}
              disabled={loading}
              autoComplete="url"
              required
            />
            <small className="help-text">Use the full base URL for your Jira instance.</small>
          </div>

          <div className="form-group">
            <label htmlFor="jira-type">Jira Type</label>
            <select
              id="jira-type"
              value={jiraType}
              onChange={(e) => setJiraType(e.target.value)}
              disabled={loading}
            >
              <option value="cloud">Jira Cloud</option>
              <option value="datacenter">Jira Data Center</option>
            </select>
            <small className="help-text">{typeConfig.helper}</small>
          </div>

          <div className="form-group">
            <label htmlFor="username">{typeConfig.usernameLabel}</label>
            <input
              id="username"
              type={typeConfig.usernameType}
              placeholder={typeConfig.usernamePlaceholder}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={loading}
              autoComplete="username"
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="password-token">{typeConfig.secretLabel}</label>
            <input
              id="password-token"
              type="password"
              placeholder={typeConfig.secretPlaceholder}
              value={passwordOrToken}
              onChange={(e) => setPasswordOrToken(e.target.value)}
              disabled={loading}
              autoComplete="current-password"
              required
            />
            <small className="help-text">{typeConfig.secretHelp}</small>
          </div>

          <div className="login-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setShowGuidance((prev) => !prev)
                if (!showGuidance && !guidance) {
                  loadGuidance()
                }
              }}
              disabled={loading}
            >
              {showGuidance ? 'Hide' : 'Show'} Setup Guidance
            </button>

            <button
              type="button"
              className="btn-secondary"
              onClick={testConnection}
              disabled={loading || testingConnection}
            >
              {testingConnection ? 'Testing...' : 'Test Connection'}
            </button>

            <button type="submit" className="btn-primary" disabled={loading}>
              {loading ? 'Connecting...' : 'Connect to Jira'}
            </button>
          </div>
        </form>

        {showGuidance && guidance && (
          <div className="guidance-box">
            <details open>
              <summary>Setup guidance</summary>

              <div className="guidance-content">{guidance}</div>
            </details>
          </div>
        )}

        {messages.length > 0 && (
          <div className="messages-log">
            <h3>Login Progress</h3>
            <div className="messages-list">
              {messages.map((msg, idx) => (
                <div key={idx} className="message-item">
                  <span className="message-icon">•</span>
                  <span>{msg}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {error && (
          <div className="error-banner">
            <span className="error-icon">⚠️</span>
            <div>
              <strong>Error</strong>
              <p>{error}</p>
            </div>
          </div>
        )}

        <div className="login-footer">
          <p className="footer-text">Credentials are validated by the backend login workflow and are not stored in the browser.</p>
        </div>
      </div>
    </div>
  )
}

