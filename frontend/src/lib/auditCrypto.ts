/**
 * Independent, in-browser checks of an audit entry (no trust in the server's /verify answer):
 *  1. SHA-256(payload_json) must equal payload_hash.
 *  2. Ed25519 signature over `${prev_hash}:${payload_hash}` must verify with /audit/pubkey.
 * WebCrypto needs a secure context (https or localhost); otherwise we report "unsupported".
 */
export type CheckResult = 'pass' | 'fail' | 'unsupported'

function subtle(): SubtleCrypto | null {
  return typeof crypto !== 'undefined' && crypto.subtle ? crypto.subtle : null
}

export async function sha256Hex(text: string): Promise<string | null> {
  const s = subtle()
  if (!s) return null
  const digest = await s.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
}

export function pemToDer(pem: string): Uint8Array<ArrayBuffer> | null {
  const m = /-----BEGIN PUBLIC KEY-----([\s\S]+?)-----END PUBLIC KEY-----/.exec(pem)
  if (!m?.[1]) return null
  try {
    const bin = atob(m[1].replace(/\s+/g, ''))
    const out = new Uint8Array(new ArrayBuffer(bin.length))
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
    return out
  } catch {
    return null
  }
}

function b64ToBytes(b64: string): Uint8Array<ArrayBuffer> | null {
  try {
    const norm = b64.trim().replace(/-/g, '+').replace(/_/g, '/')
    const bin = atob(norm + '='.repeat((4 - (norm.length % 4)) % 4))
    const out = new Uint8Array(new ArrayBuffer(bin.length))
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
    return out
  } catch {
    return null
  }
}

export async function checkPayloadHash(payloadJson: string, payloadHash: string): Promise<CheckResult> {
  const h = await sha256Hex(payloadJson)
  if (h == null) return 'unsupported'
  return h === payloadHash.trim().toLowerCase() ? 'pass' : 'fail'
}

export async function checkSignature(publicKeyPem: string, prevHash: string, payloadHash: string, signatureB64: string): Promise<CheckResult> {
  const s = subtle()
  const der = pemToDer(publicKeyPem)
  const sig = b64ToBytes(signatureB64)
  if (!s || !der) return 'unsupported'
  if (!sig) return 'fail'
  let key: CryptoKey
  try {
    key = await s.importKey('spki', der, { name: 'Ed25519' }, false, ['verify'])
  } catch {
    return 'unsupported' // browser without Ed25519 support, or a non-Ed25519 key
  }
  try {
    const ok = await s.verify({ name: 'Ed25519' }, key, sig, new TextEncoder().encode(`${prevHash}:${payloadHash}`))
    return ok ? 'pass' : 'fail'
  } catch {
    return 'fail'
  }
}
