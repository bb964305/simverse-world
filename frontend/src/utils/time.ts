// ─── UTC-safe timestamp parsing (Kickoff V4 发现③) ─────────────────
//
// The backend serializes datetimes with `isoformat()` on naive-UTC values,
// so most timestamps arrive WITHOUT a timezone suffix ("2026-07-13T08:00:00").
// `new Date(s)` treats such strings as *local* time, shifting every display
// by the viewer's UTC offset. Pin naive strings to UTC before parsing.

/** Matches an explicit timezone suffix: Z, +08:00, -0530, … */
const TZ_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i

/**
 * Parse a backend timestamp as UTC.
 * Strings without a timezone suffix get `Z` appended first; strings that
 * already carry an offset are parsed as-is.
 */
export function parseUTC(iso: string): Date {
  return new Date(TZ_SUFFIX.test(iso) ? iso : `${iso}Z`)
}
