import { MOCK, MOCK_ALLOWED } from '../lib/mockMode'
import { ApiError } from './errors'
import type {
  AuditEntryView,
  AuditVerifyResult,
  HealthView,
  JobView,
  LegalDraftRequest,
  LegalDraftResponse,
  ReportConfirmRequest,
  ReportConfirmResponse,
  ReportDraftPackage,
  VerifyAccepted,
  VerifyRequest,
  WithMeta,
} from './types'

const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/+$/, '')
const API_KEY = import.meta.env.VITE_API_KEY

export function apiUrl(path: string): string {
  return `${BASE}${path}`
}

const enc = encodeURIComponent

/** Server-rendered resources linked directly (download / img src). */
export const links = {
  events: (jobId: string) => apiUrl(`/verify/${enc(jobId)}/events`),
  auditPdf: (jobId: string) => apiUrl(`/audit/${enc(jobId)}/pdf`),
  publicKey: () => apiUrl('/audit/pubkey'),
  reportCard: (jobId: string) => apiUrl(`/report/${enc(jobId)}/card.png`),
  bundleZip: (jobId: string) => apiUrl(`/report/${enc(jobId)}/bundle.zip`),
}

type Method = 'GET' | 'POST'

// Loaded lazily and only when mock mode is allowed at compile time, so production bundles never
// contain fixtures.
type MockModule = typeof import('../mocks/server')
export const loadMock: () => Promise<MockModule> = __MOCK_ALLOWED__
  ? () => import('../mocks/server')
  : () => Promise.reject(new Error('Mock mode is not available in this build'))

async function request<T>(method: Method, path: string, body?: unknown): Promise<WithMeta<T>> {
  if (MOCK_ALLOWED && MOCK) {
    const mock = await loadMock()
    return (await mock.handleRequest(method, path, body)) as WithMeta<T>
  }
  const headers: Record<string, string> = { Accept: 'application/json, application/problem+json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (API_KEY) headers['X-API-Key'] = API_KEY
  let res: Response
  try {
    res = await fetch(apiUrl(path), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
    })
  } catch {
    throw new ApiError(0, 'Network error: the VeriLoop server could not be reached.')
  }
  const ct = res.headers.get('content-type') ?? ''
  let data: unknown = null
  let text = ''
  if (ct.includes('json')) data = await res.json().catch(() => null)
  else text = await res.text().catch(() => '')
  if (!res.ok) throw ApiError.fromResponse(res.status, data, text)
  if (data === null) throw new ApiError(res.status, `Unexpected non-JSON response from ${path}`)
  return data as WithMeta<T>
}

/** Multipart POST /verify with a file. Uses XHR for upload progress. */
function uploadVerify(
  file: File,
  fields: Omit<VerifyRequest, 'upload_id'>,
  onProgress?: (fraction: number) => void,
): Promise<WithMeta<VerifyAccepted>> {
  if (MOCK_ALLOWED && MOCK) {
    return loadMock().then((m) => m.mockUpload(file, fields, onProgress))
  }
  const form = new FormData()
  form.append('file', file, file.name)
  for (const [k, v] of Object.entries(fields)) {
    if (v !== undefined && v !== null && v !== '') form.append(k, String(v))
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', apiUrl('/verify'))
    xhr.setRequestHeader('Accept', 'application/json, application/problem+json')
    if (API_KEY) xhr.setRequestHeader('X-API-Key', API_KEY)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
    }
    xhr.onerror = () => reject(new ApiError(0, 'Network error while uploading the file.'))
    xhr.onload = () => {
      let data: unknown = null
      try {
        data = JSON.parse(xhr.responseText)
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data as WithMeta<VerifyAccepted>)
      else reject(ApiError.fromResponse(xhr.status, data, xhr.responseText))
    }
    xhr.send(form)
  })
}

async function publicKeyPem(): Promise<string> {
  if (MOCK_ALLOWED && MOCK) return (await loadMock()).mockPublicKeyPem()
  const res = await fetch(links.publicKey(), { headers: { Accept: 'application/x-pem-file, text/plain, application/json' } })
  if (!res.ok) throw ApiError.fromResponse(res.status, null, await res.text().catch(() => ''))
  const text = await res.text()
  if (text.includes('BEGIN PUBLIC KEY')) {
    // Either raw PEM, or JSON with the PEM in some field.
    const m = /-----BEGIN PUBLIC KEY-----[\s\S]+?-----END PUBLIC KEY-----/.exec(text.replace(/\\n/g, '\n'))
    if (m) return m[0]
  }
  throw new ApiError(res.status, 'The public key response did not contain a PEM public key.')
}

export const api = {
  health: () => request<HealthView>('GET', '/health'),
  submit: (req: VerifyRequest) => request<VerifyAccepted>('POST', '/verify', req),
  submitUpload: uploadVerify,
  job: (jobId: string) => request<JobView>('GET', `/verify/${enc(jobId)}`),
  prepareReport: (jobId: string) => request<ReportDraftPackage>('POST', `/report/${enc(jobId)}`),
  confirmReport: (jobId: string, body: ReportConfirmRequest) =>
    request<ReportConfirmResponse>('POST', `/report/${enc(jobId)}/confirm`, body),
  legalDraft: (jobId: string, body: LegalDraftRequest) =>
    request<LegalDraftResponse>('POST', `/legal/${enc(jobId)}/draft`, body),
  audit: (jobId: string) => request<AuditEntryView>('GET', `/audit/${enc(jobId)}`),
  auditVerify: (jobId: string) => request<AuditVerifyResult>('GET', `/audit/${enc(jobId)}/verify`),
  publicKeyPem,
}
