import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { $confirmRequest, type PendingModal, runConfirm, settleConfirm, settlePrompt } from '@/store/confirm'

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
      busyLabel={shown.kind === 'confirm' ? shown.busyLabel : undefined}
      cancelLabel={shown.cancelLabel}
      confirmLabel={shown.confirmLabel}
      description={shown.description}
      destructive={shown.kind === 'confirm' ? shown.destructive : undefined}
      dismissOnConfirm={shown.kind === 'prompt' || !shown.onConfirm}
      doneLabel={shown.kind === 'confirm' ? shown.doneLabel : undefined}
      key={shown.id}
      onClose={() => {
        if (shown.kind === 'prompt') {
          settlePrompt(null)
          return
        }
        settleConfirm(shown.phase === 'done', shown)
      }}
      onConfirm={value => {
        if (shown.kind === 'prompt') {
          settlePrompt(value ?? null)
          return
        }
        return runConfirm(shown)
      }}
      open={request !== null}
      textInput={shown.kind === 'prompt' ? { placeholder: shown.placeholder } : undefined}
      title={shown.title}
    >
      {shown.kind === 'confirm' && shown.details && (
        <dl className="space-y-3 text-xs">
          {shown.details.map(({ label, value }) => (
            <div className="grid grid-cols-[5rem_minmax(0,1fr)] gap-3" key={label}>
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="m-0 whitespace-pre-wrap break-words text-foreground">{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </ConfirmDialog>
  )
}
