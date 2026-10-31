import { Link } from 'react-router'
import { Icon, type IconName } from '../components/Icon'
import { usePageTitle } from '../hooks/usePageTitle'

const LOOP: Array<{ icon: IconName; title: string; text: string }> = [
  { icon: 'scale', title: 'Verdict', text: 'Cited TRUE / FALSE / MANIPULATED, or an honest abstain. Never without two independent sources.' },
  { icon: 'layers', title: 'Evidence bundle', text: 'verdict.json, sources, forensic scores, media hashes, timestamps: one portable package.' },
  { icon: 'flag', title: 'Platform report', text: 'Pre-filled report for X, Instagram, YouTube…, or a report card for WhatsApp and Telegram. You press submit.' },
  { icon: 'gavel', title: 'Legal escalation', text: 'For deepfake victims: an India / EU / US draft notice. Draft only, never sent by VeriLoop.' },
  { icon: 'chain', title: 'Tamper-evident audit', text: 'Every run is hash-chained and Ed25519-signed. Anyone can verify it, live.' },
]

const GUARDRAILS: Array<{ code: string; text: string }> = [
  { code: 'G1 · G4', text: 'No citation, no verdict: at least two independent publishers, or VeriLoop abstains.' },
  { code: 'G2', text: 'Below the calibrated confidence threshold, the answer is “Unverifiable”, not a guess.' },
  { code: 'G8', text: '“No evidence found” is never shown as “false”: grey, dashed, and labelled as such.' },
  { code: 'G3', text: 'Every tool failure is logged and named in the result. Nothing is invented to fill the gap.' },
  { code: 'G5', text: 'Reports and legal drafts are never auto-submitted. A person confirms every step.' },
  { code: 'G7', text: 'Resolved model ids and tool versions are written into every audit record.' },
  { code: 'G9', text: 'Claims about private individuals need forensic proof plus two sources before any legal draft.' },
  { code: 'G6', text: 'Telegram message content is deleted after 24 hours; only anonymised metadata is kept.' },
]

export default function AboutPage() {
  usePageTitle('How it works')
  return (
    <div className="space-y-8">
      <div className="max-w-3xl">
        <p className="eyebrow">Why VeriLoop</p>
        <h1 className="text-3xl font-extrabold tracking-tight sm:text-4xl">Detection is not the end. VeriLoop closes the loop.</h1>
        <p className="mt-2 text-lg text-muted">
          Most tools stop at “this looks fake”. VeriLoop owns the enforcement layer: from a cited verdict to a platform
          report, a legal draft for the person harmed, and a public, verifiable audit trail.
        </p>
      </div>

      <section aria-labelledby="loop-title">
        <h2 id="loop-title" className="sr-only">
          The closed loop
        </h2>
        <ol className="grid gap-2 lg:grid-cols-5 lg:gap-0">
          {LOOP.map((s, i) => (
            <li key={s.title} className="flex flex-col items-stretch lg:flex-row lg:items-center">
              <div className="card flex flex-1 gap-3 p-4 lg:h-full lg:flex-col lg:gap-2">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-primary text-primary-fg">
                  <Icon name={s.icon} size={20} />
                </span>
                <div>
                  <p className="text-xs font-bold text-muted">Step {i + 1}</p>
                  <h3 className="font-bold">{s.title}</h3>
                  <p className="mt-1 text-sm text-muted">{s.text}</p>
                </div>
              </div>
              {i < LOOP.length - 1 ? (
                <span className="flex justify-center py-0.5 text-muted lg:px-1 lg:py-0" aria-hidden="true">
                  <Icon name="arrow-down" size={18} className="lg:-rotate-90" />
                </span>
              ) : null}
            </li>
          ))}
        </ol>
        <p className="mt-3 flex items-center gap-2 text-sm text-muted">
          <Icon name="activity" size={16} /> The audit record links back to the verdict, so every action stays traceable to its evidence.
        </p>
      </section>

      <section aria-labelledby="guard-title" className="card p-4 sm:p-6">
        <h2 id="guard-title" className="mb-3 flex items-center gap-2 text-lg font-bold">
          <Icon name="shield" /> Guardrails, enforced in code
        </h2>
        <ul className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
          {GUARDRAILS.map((g) => (
            <li key={g.code} className="flex gap-3 text-sm">
              <span className="w-16 shrink-0 font-mono text-xs font-bold text-muted">{g.code}</span>
              <span>{g.text}</span>
            </li>
          ))}
        </ul>
      </section>

      <div className="flex flex-wrap gap-3">
        <Link to="/" className="btn btn-primary">
          <Icon name="search" size={18} /> Check a claim
        </Link>
      </div>
    </div>
  )
}
