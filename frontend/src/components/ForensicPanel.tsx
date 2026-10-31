import type { AbstainBand, ForensicResult, ForensicStatus, InputSummary, MediaHash } from '../api/types'
import { formatMs, humanize, num, toolLabel } from '../lib/format'
import { Icon, type IconName } from './Icon'
import { Callout, EmptyNote, KeyValue, Section } from './ui'

export const C2PA_ABSENCE_SENTENCE = 'Absence of C2PA credentials is not evidence of manipulation.'

const STATUS: Record<ForensicStatus, { text: string; icon: IconName; cls: string }> = {
  real: { text: 'No manipulation detected', icon: 'check-circle', cls: 'border-true-line bg-true-soft text-true-fg' },
  manipulated: { text: 'Manipulation detected', icon: 'scan-face', cls: 'border-manip-line bg-manip-soft text-manip-fg' },
  inconclusive: { text: 'Inconclusive (abstain band)', icon: 'help-circle', cls: 'border-abstain-line bg-abstain-soft text-abstain-fg' },
  unavailable: { text: 'Unavailable', icon: 'ban', cls: 'border-dashed border-neutral-line bg-neutral-soft text-neutral-fg' },
}

const PROVIDER: Record<string, string> = { local: 'local model', hf_inference: 'HF Inference', sightengine: 'commercial API' }

/** One horizontal 0..1 track with the abstain band shaded and a score mark. */
function ScoreTrack({ score, band, strong }: { score: number | null; band: AbstainBand; strong?: boolean }) {
  return (
    <div className="relative h-5" aria-hidden="true">
      <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border" />
      <div
        className="absolute inset-y-0 border-x border-abstain-line/60 bg-band"
        style={{ left: `${band.low * 100}%`, width: `${(band.high - band.low) * 100}%` }}
      />
      {score != null ? (
        <div
          className={`absolute left-0 top-1/2 -translate-y-1/2 rounded-r ${strong ? 'h-3 bg-fg' : 'h-2 bg-primary'}`}
          style={{ width: `${Math.max(0, Math.min(1, score)) * 100}%` }}
        />
      ) : null}
    </div>
  )
}

function Axis({ band }: { band: AbstainBand }) {
  const ticks = [0, band.low, band.high, 1]
  return (
    <div className="relative h-4 text-[11px] tabular-nums text-muted" aria-hidden="true">
      {ticks.map((t, i) => (
        <span key={i} className={`absolute ${i === 0 ? '' : i === ticks.length - 1 ? '-translate-x-full' : '-translate-x-1/2'}`} style={{ left: `${t * 100}%` }}>
          {num(t, 2)}
        </span>
      ))}
    </div>
  )
}

function thumbUrl(h: MediaHash): string | null {
  for (const k of ['thumbnail_url', 'thumb_url', 'url']) {
    const v = h[k]
    if (typeof v === 'string' && /^(https?:\/\/|\/)/.test(v)) return v
  }
  return null
}

function Frames({ hashes }: { hashes: MediaHash[] }) {
  if (!hashes.length) return null
  return (
    <div>
      <h3 className="eyebrow mb-2">Analyzed media ({hashes.length})</h3>
      <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {hashes.map((h, i) => {
          const src = thumbUrl(h)
          return (
            <li key={String(h.id ?? i)} className="overflow-hidden rounded-md border border-border bg-surface-2 text-xs">
              {src ? (
                <img src={src} alt={`Frame ${h.frame_ts != null ? `at ${h.frame_ts} s` : String(h.id ?? i + 1)}`} loading="lazy" className="aspect-video w-full object-cover" />
              ) : (
                <div className="flex aspect-video items-center justify-center text-muted" aria-hidden="true">
                  <Icon name="film" size={22} />
                </div>
              )}
              <div className="space-y-0.5 p-1.5">
                <p className="font-semibold">
                  {String(h.id ?? `#${i + 1}`)} {h.kind ? <span className="font-normal text-muted">{humanize(String(h.kind))}</span> : null}
                </p>
                {h.frame_ts != null ? <p className="text-muted">t = {h.frame_ts} s</p> : null}
                {h.phash ? (
                  <p className="truncate font-mono" title={`pHash ${h.phash}`}>
                    pHash {h.phash}
                  </p>
                ) : null}
                {h.sha256 ? (
                  <p className="truncate font-mono text-muted" title={`SHA-256 ${h.sha256}`}>
                    {h.sha256.slice(0, 12)}…
                  </p>
                ) : null}
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export function ForensicPanel({ forensic, input }: { forensic: ForensicResult | null; input: InputSummary | null }) {
  if (!forensic) {
    return (
      <Section id="forensics" title="Media forensics" icon="scan-face">
        <EmptyNote>Forensics did not run for this input.</EmptyNote>
      </Section>
    )
  }
  const st = STATUS[forensic.status] ?? STATUS.unavailable
  const band = forensic.band
  const scored = forensic.models.filter((m) => m.raw_score != null)
  const md = forensic.metadata
  const delta = forensic.disagreement
  const sampling = forensic.sampling ?? input?.sampling ?? null

  return (
    <Section
      id="forensics"
      title="Media forensics"
      icon="scan-face"
      subtitle="Deepfake detector ensemble. Each score is the model's probability that the media is fake."
      actions={
        <span className={`chip ${st.cls}`}>
          <Icon name={st.icon} size={14} />
          {st.text}
        </span>
      }
    >
      <div className="space-y-5">
        {forensic.models.length ? (
          <figure>
            <figcaption className="mb-2 text-sm text-muted">
              Shaded band [{num(band.low)}, {num(band.high)}] = abstain zone: scores there never count as evidence of
              manipulation.
            </figcaption>
            <ul className="space-y-2.5">
              {forensic.models.map((m) => (
                <li key={m.model_id} className="grid grid-cols-1 gap-x-3 gap-y-0.5 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_3.5rem] sm:items-center">
                  <div className="min-w-0 text-sm">
                    <p className="truncate font-mono font-semibold" title={`${m.model_id}${m.model_version ? ` @ ${m.model_version}` : ''}`}>
                      {m.model_id}
                    </p>
                    <p className="text-xs text-muted">
                      {PROVIDER[m.provider] ?? m.provider} · weight {num(m.weight)}
                      {m.frames_scored ? ` · ${m.frames_scored} frame${m.frames_scored === 1 ? '' : 's'}` : ''}
                      {m.latency_ms ? ` · ${formatMs(m.latency_ms)}` : ''}
                    </p>
                  </div>
                  {m.raw_score != null ? (
                    <>
                      <div title={`${m.model_id}: P(fake) = ${num(m.raw_score)}`}>
                        <ScoreTrack score={m.raw_score} band={band} />
                      </div>
                      <p className="text-sm font-semibold tabular-nums sm:text-right">{num(m.raw_score)}</p>
                    </>
                  ) : (
                    <p className="text-sm text-false-fg sm:col-span-2">
                      <Icon name="alert" size={14} className="mr-1 inline" />
                      No score: {m.error ?? 'model unavailable'}
                    </p>
                  )}
                </li>
              ))}
              <li className="grid grid-cols-1 gap-x-3 gap-y-0.5 border-t border-border pt-2.5 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_3.5rem] sm:items-center">
                <p className="text-sm font-bold">Weighted ensemble</p>
                <div title={`Ensemble P(fake) = ${num(forensic.ensemble_score)}`}>
                  <ScoreTrack score={forensic.ensemble_score} band={band} strong />
                </div>
                <p className="text-sm font-extrabold tabular-nums sm:text-right">{num(forensic.ensemble_score)}</p>
              </li>
              <li className="hidden sm:grid sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_3.5rem] sm:gap-x-3">
                <span />
                <Axis band={band} />
                <span />
              </li>
            </ul>
            {delta != null && scored.length > 1 ? (
              <p className={`mt-3 flex items-start gap-2 text-sm ${delta > band.delta ? 'text-abstain-fg' : 'text-muted'}`}>
                <Icon name={delta > band.delta ? 'alert' : 'check-circle'} size={16} className="mt-0.5" />
                <span>
                  Disagreement (max − min) <strong className="tabular-nums">{num(delta)}</strong>{' '}
                  {delta > band.delta
                    ? `exceeds the allowed ${num(band.delta)}: the models disagree, so the forensic branch is inconclusive.`
                    : `is within the allowed ${num(band.delta)}.`}
                </span>
              </p>
            ) : null}
          </figure>
        ) : (
          <EmptyNote>
            No detector scores. {forensic.status === 'unavailable' ? 'Visual forensics were unavailable for this input.' : ''}
          </EmptyNote>
        )}

        <KeyValue
          items={[
            ['Branch status', humanize(forensic.branch_status)],
            ['Frames analyzed', `${forensic.frames_analyzed}${forensic.faces_found ? ` · ${forensic.faces_found} face(s) found` : ''}`],
            sampling ? ['Sampling', sampling.note ?? `${sampling.frames} frames at ${sampling.fps} fps${sampling.duration_s ? ` of ${Math.round(sampling.duration_s)} s` : ''}${sampling.capped ? ' (cap reached)' : ''}`] : null,
            forensic.asr_ocr_mismatch != null ? ['Speech vs on-screen text', `mismatch ${num(forensic.asr_ocr_mismatch)} (higher = spoken words disagree with on-screen text)`] : null,
            forensic.synthetic_speech_score != null ? ['Synthetic speech score', num(forensic.synthetic_speech_score)] : null,
            forensic.latency_ms ? ['Branch time', formatMs(forensic.latency_ms)] : null,
          ]}
        />

        <div className="rounded-lg border border-border bg-surface-2 p-3">
          <h3 className="mb-2 flex items-center gap-2 font-semibold">
            <Icon name="shield" size={18} /> Metadata & Content Credentials (bonus signal only)
          </h3>
          {md ? (
            <KeyValue
              items={[
                ['EXIF', md.exif_present ? `present${md.exif_software ? ` (software: ${md.exif_software})` : ''}` : `absent${md.exif_stripped_suspected ? ' (stripping suspected)' : ''}`],
                md.container ? ['Container', md.container] : null,
                md.reencode_hints.length ? ['Re-encoding', md.reencode_hints.join('; ')] : null,
                ['C2PA', md.c2pa_present ? `present: signer ${md.c2pa_signer ?? 'unknown'}, issuer ${md.c2pa_issuer ?? 'unknown'}` : 'no Content Credentials found'],
                md.c2pa_actions.length ? ['C2PA actions', md.c2pa_actions.join(', ')] : null,
              ]}
            />
          ) : (
            <p className="text-sm text-muted">No metadata report for this input.</p>
          )}
          <p className="mt-2 text-sm font-semibold">{C2PA_ABSENCE_SENTENCE}</p>
          {md?.c2pa_note && !/not evidence of manipulation/i.test(md.c2pa_note) ? (
            <p className="mt-1 text-xs text-muted">{md.c2pa_note}</p>
          ) : null}
        </div>

        <Frames hashes={input?.media_hashes ?? []} />

        {forensic.notes.length ? (
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {forensic.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        ) : null}
        {forensic.errors.length ? (
          <Callout tone="neutral" title="Logged forensic tool failures">
            <ul className="space-y-0.5">
              {forensic.errors.map((e, i) => (
                <li key={i}>
                  {toolLabel(e.tool)}: {e.error_type}: {e.message}
                </li>
              ))}
            </ul>
          </Callout>
        ) : null}
      </div>
    </Section>
  )
}
