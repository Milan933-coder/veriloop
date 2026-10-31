export const MAX_UPLOAD_MB = 50
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

/** Client-side check before POST /verify multipart (the server enforces MAX_UPLOAD_MB too). */
export function validateUpload(file: { size: number; type: string; name: string }): string | null {
  const isMedia = file.type.startsWith('image/') || file.type.startsWith('video/') || /\.(jpe?g|png|webp|gif|heic|mp4|mov|webm|mkv|3gp)$/i.test(file.name)
  if (!isMedia) return 'Please choose an image or a video file.'
  if (file.size > MAX_UPLOAD_BYTES) return `This file is ${(file.size / 1024 / 1024).toFixed(1)} MB. The limit is ${MAX_UPLOAD_MB} MB.`
  if (file.size === 0) return 'This file is empty.'
  return null
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
