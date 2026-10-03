import { atom } from 'nanostores'

export interface ConfirmRequest {
  title: string
  description?: string
  confirmLabel?: string
  busyLabel?: string
  doneLabel?: string
  details?: { label: string; value: string }[]
  onConfirm?: () => Promise<void>
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

export interface PendingConfirm extends ConfirmRequest {
  kind: 'confirm'
  id: number
  resolve: (confirmed: boolean) => void
  phase?: 'running' | 'done'
}

interface PendingPrompt extends PromptRequest {
  kind: 'prompt'
  id: number
  resolve: (value: string | null) => void
}

export type PendingModal = PendingConfirm | PendingPrompt

export const $confirmRequest = atom<null | PendingModal>(null)
let nextRequestId = 0

function dismissUnanswered(confirmed: boolean): void {
  const pending = $confirmRequest.get()

  if (!pending || (pending.kind === 'confirm' && pending.phase)) {
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
// inline the way window.confirm gave it — `if (!ok) return`. Pass onConfirm
// to use the shared dialog's progress, completion and inline error states.
export function confirm(request: ConfirmRequest): Promise<boolean> {
  // An action already in progress must not lose its owner or completion UI.
  const current = $confirmRequest.get()
  if (current?.kind === 'confirm' && current.phase) {
    return Promise.resolve(false)
  }

  // One modal at a time: a second ask supersedes an unanswered question.
  dismissUnanswered(false)

  return new Promise<boolean>(resolve => {
    $confirmRequest.set({ ...request, kind: 'confirm', id: ++nextRequestId, resolve })
  })
}

/** Run the captured request, not a replacement that arrived during I/O. */
export async function runConfirm(pending: PendingConfirm): Promise<void> {
  if ($confirmRequest.get() !== pending || pending.phase) {
    return
  }

  if (!pending.onConfirm) {
    settleConfirm(true, pending)

    return
  }

  pending.phase = 'running'

  try {
    await pending.onConfirm()
    pending.phase = 'done'
  } catch (error) {
    delete pending.phase
    throw error
  }
}

/** Answer the open confirm, if there still is one. A prompt answers null.
 *  Idempotent. */
export function settleConfirm(confirmed: boolean, expected?: PendingConfirm): void {
  const pending = $confirmRequest.get()

  if (!pending || (expected && pending !== expected)) {
    return
  }

  if (pending.kind === 'prompt') {
    if (expected) {
      return
    }
    $confirmRequest.set(null)
    pending.resolve(null)
    return
  }

  if (pending.phase === 'running') {
    return
  }

  $confirmRequest.set(null)
  pending.resolve(confirmed)
}

/** Text-input sibling of `confirm`. Cancel and a blank submit resolve null.
 *  Electron has no `window.prompt` (it returns null or throws), so a reason
 *  collected for a pause has to come through this dialog. */
export function promptText(request: PromptRequest): Promise<string | null> {
  const current = $confirmRequest.get()
  if (current?.kind === 'confirm' && current.phase) {
    return Promise.resolve(null)
  }

  dismissUnanswered(false)

  return new Promise<string | null>(resolve => {
    $confirmRequest.set({ ...request, kind: 'prompt', id: ++nextRequestId, resolve })
  })
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
