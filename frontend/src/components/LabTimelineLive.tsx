import type { LabDisplay } from '../services/labState'
import { useReducedMotion } from '../hooks/useReducedMotion'
import { LabTimeline } from './LabTimeline'

// Thin live wrapper: reads the OS prefers-reduced-motion setting and feeds it
// to the pure LabTimeline. LabTimeline stays framework-only and prop-driven so
// it remains trivially unit-testable; this is the single seam that touches the
// browser media query. Use this in app code; use LabTimeline directly in tests.
export function LabTimelineLive({ display }: { display: LabDisplay }) {
  const reducedMotion = useReducedMotion()
  return <LabTimeline display={display} reducedMotion={reducedMotion} />
}
