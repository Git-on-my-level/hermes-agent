import { atom } from 'nanostores'

export interface ConfirmRequest {
  title: string
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
}

export interface PromptRequest {
  title: string
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  placeholder?: string
}

interface PendingConfirm extends ConfirmRequest {
  kind: 'confirm'
  resolve: (confirmed: boolean) => void
}

interface PendingPrompt extends PromptRequest {
  kind: 'prompt'
  resolve: (value: string | null) => void
}

export type PendingModal = PendingConfirm | PendingPrompt

export const $confirmRequest = atom<null | PendingModal>(null)

function dismissOpen(confirmed: boolean): void {
  const pending = $confirmRequest.get()

  if (!pending) {
    return
  }

  $confirmRequest.set(null)

  if (pending.kind === 'prompt') {
    pending.resolve(null)
    return
  }

  pending.resolve(confirmed)
}

// Imperative front door to ConfirmDialog, for handlers that want the answer
// inline the way window.confirm gave it — `if (!ok) return`. A surface that
// wants the busy → done beat or an inline error should mount <ConfirmDialog>
// itself and hand it the async onConfirm.
export function confirm(request: ConfirmRequest): Promise<boolean> {
  // One modal at a time: a second ask supersedes the first, which answers no.
  dismissOpen(false)

  return new Promise<boolean>(resolve => {
    $confirmRequest.set({ ...request, kind: 'confirm', resolve })
  })
}

/** Text-input sibling of `confirm`. Cancel and a blank submit resolve null.
 *  Electron has no `window.prompt` (it returns null or throws), so a reason
 *  collected for a pause has to come through this dialog. */
export function promptText(request: PromptRequest): Promise<string | null> {
  dismissOpen(false)

  return new Promise<string | null>(resolve => {
    $confirmRequest.set({ ...request, kind: 'prompt', resolve })
  })
}

/** Answer the open confirm, if there still is one. A prompt answers null.
 *  Idempotent. */
export function settleConfirm(confirmed: boolean): void {
  dismissOpen(confirmed)
}

/** Answer the open prompt with the trimmed text, or null when it is blank. */
export function settlePrompt(value: string | null): void {
  const pending = $confirmRequest.get()

  if (!pending || pending.kind !== 'prompt') {
    return
  }

  $confirmRequest.set(null)
  const text = typeof value === 'string' ? value.trim() : ''
  pending.resolve(text || null)
}
