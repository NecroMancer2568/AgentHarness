import React, { Component, ErrorInfo, ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'

type Props = { children: ReactNode }
type State = { error: Error | null }
class AppErrorBoundary extends Component<Props, State> {
  state: State = { error: null }
  static getDerivedStateFromError(error: Error): State { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('AgentHarness UI render failed', error, info) }
  render() {
    if (this.state.error) return <div className="boot-screen boot-error"><div className="boot-mark">AH</div><h1>AgentHarness could not render</h1><p>The frontend encountered a runtime error.</p><pre>{this.state.error.message}</pre><small>Open the browser console and terminal for the full stack trace. Confirm dependencies are installed with npm install, then restart npm run dev.</small></div>
    return this.props.children
  }
}

const root = document.getElementById('root')
if (!root) throw new Error('Missing #root element in index.html')
createRoot(root).render(<React.StrictMode><AppErrorBoundary><App /></AppErrorBoundary></React.StrictMode>)
