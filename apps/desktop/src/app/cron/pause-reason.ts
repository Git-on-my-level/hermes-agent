import { promptText } from '@/store/confirm'

/** Why a cron job is being paused. Electron has no `window.prompt`, so the
 *  reason is collected through the app confirm modal. Cancel and a blank
 *  answer resolve null and the caller leaves the job running. */
export function askCronPauseReason(copy: { confirmLabel: string; prompt: string }): Promise<string | null> {
  return promptText({
    confirmLabel: copy.confirmLabel,
    placeholder: copy.prompt,
    title: copy.prompt
  })
}
