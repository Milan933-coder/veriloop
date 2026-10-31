import type { EvidenceItem, JobView } from '../api/types'

/** Every citable item of the job, deduplicated by id (sources, provenance pages, debate follow-ups). */
export function allEvidence(job: JobView): EvidenceItem[] {
  const seen = new Set<string>()
  const out: EvidenceItem[] = []
  for (const it of [...job.sources, ...(job.provenance?.items ?? []), ...(job.debate?.followup_items ?? [])]) {
    if (!seen.has(it.id)) {
      seen.add(it.id)
      out.push(it)
    }
  }
  return out
}
