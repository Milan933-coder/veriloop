"""User-visible caveat strings (BUILD_SPEC §11: every caveat goes into `labels`). Kept verbatim."""

LOGIN_WALLED = "Media inaccessible (login-walled) — analyzed caption only"
VISUAL_UNAVAILABLE = "Visual forensics unavailable — analyzed caption only"
THUMBNAIL_ONLY = "Only a platform thumbnail was accessible — visual forensics limited"
PAGE_CAPTURE = "Page rendered in a headless browser (metadata fallback)"
PARTIAL_DOWNLOAD = "Long video: only the first {max_s} s were downloaded and sampled"
UPLOAD_TOO_LARGE = "Upload exceeds MAX_UPLOAD_MB ({max_mb} MB) — analyzed text only"
