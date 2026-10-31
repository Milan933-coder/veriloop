import { Link } from 'react-router'
import { usePageTitle } from '../hooks/usePageTitle'

export default function NotFoundPage() {
  usePageTitle('Page not found')
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Page not found</h1>
      <p className="text-muted">This page does not exist. Result links look like /j/&lt;job id&gt; and audit links like /a/&lt;job id&gt;.</p>
      <Link to="/" className="btn btn-primary">
        Check a claim
      </Link>
    </div>
  )
}
