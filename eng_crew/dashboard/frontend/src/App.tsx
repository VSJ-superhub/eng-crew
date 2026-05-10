import React from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import RunDetail from './pages/RunDetail'
import Backlog from './pages/Backlog'
import Projects from './pages/Projects'
import ProjectTasks from './pages/ProjectTasks'
import Intake from './pages/Intake'
import Stacks from './pages/Stacks'
import Review from './pages/Review'

interface ErrorBoundaryState {
  hasError: boolean
  message: string
}

class ErrorBoundary extends React.Component<React.PropsWithChildren, ErrorBoundaryState> {
  constructor(props: React.PropsWithChildren) {
    super(props)
    this.state = { hasError: false, message: '' }
  }

  static getDerivedStateFromError(e: Error): ErrorBoundaryState {
    return { hasError: true, message: e.message }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('ErrorBoundary caught:', error, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-gray-950 flex items-center justify-center p-6">
          <div className="rounded-xl bg-gray-900 border border-gray-700 shadow-sm p-8 max-w-md w-full">
            <h1 className="text-lg font-semibold text-white mb-2">Something went wrong</h1>
            <p className="text-gray-400 text-sm mb-6">{this.state.message}</p>
            <button
              onClick={() => window.location.reload()}
              className="rounded-xl bg-orange-500 hover:bg-orange-600 text-white px-4 py-2 text-sm font-medium transition-colors"
            >
              Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          <Route path='/' element={<Layout />}>
            <Route index element={<Dashboard />} />
            <Route path='run/:id' element={<RunDetail />} />
            <Route path='backlog' element={<Backlog />} />
            <Route path='projects' element={<Projects />} />
            <Route path='projects/:id/tasks' element={<ProjectTasks />} />
            <Route path='intake' element={<Intake />} />
            <Route path='stacks' element={<Stacks />} />
            <Route path='review' element={<Review />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  )
}
