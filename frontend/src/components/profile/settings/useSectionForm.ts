import { useState } from 'react'

// Shared submit state for a settings section: tracks the saving flag while
// the async save runs and shows the "saved" indicator for 2 seconds on
// success. Errors are intentionally swallowed (same behavior as before the
// SettingsPanel split).
export function useSectionForm(save: () => Promise<void>) {
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const handleSave = async () => {
    setSaving(true)
    setSaved(false)
    try {
      await save()
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch { /* ignore */ }
    finally { setSaving(false) }
  }

  return { saving, saved, handleSave }
}
