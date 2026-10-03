import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { $confirmRequest, type PendingModal, settleConfirm, settlePrompt } from '@/store/confirm'

// The one mount point for `confirm()` from @/store/confirm. Mounted once at the
// shell, the way NotificationStack backs notify().
export function ConfirmHost() {
  const request = useStore($confirmRequest)
  // The atom clears the moment the question is answered, but Radix still has a
  // close animation to play — hold the copy so the dialog doesn't blank mid-fade.
  const [shown, setShown] = useState<null | PendingModal>(request)

  useEffect(() => {
    if (request) {
      setShown(request)
    }
  }, [request])

  if (!shown) {
    return null
  }

  return (
    <ConfirmDialog
      cancelLabel={shown.cancelLabel}
      confirmLabel={shown.confirmLabel}
      description={shown.description}
      destructive={shown.kind === 'confirm' ? shown.destructive : undefined}
      // The caller does the work once it has its answer, so there is nothing
      // here to keep the dialog open for.
      dismissOnConfirm
      onClose={() => settleConfirm(false)}
      onConfirm={value => {
        if (shown.kind === 'prompt') {
          settlePrompt(value ?? null)
          return
        }
        settleConfirm(true)
      }}
      open={request !== null}
      textInput={shown.kind === 'prompt' ? { placeholder: shown.placeholder } : undefined}
      title={shown.title}
    />
  )
}
