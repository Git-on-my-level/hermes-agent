import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ConfirmHost } from '@/components/confirm-host'
import { $confirmRequest } from '@/store/confirm'

import { askCronPauseReason } from './pause-reason'

afterEach(() => {
  cleanup()
  $confirmRequest.set(null)
})

const copy = { confirmLabel: 'Pause', prompt: 'Why is this job paused?' }

describe('askCronPauseReason', () => {
  it('collects the reason through the confirm modal', async () => {
    render(<ConfirmHost />)

    const pending = askCronPauseReason(copy)
    const field = await screen.findByRole('textbox', { name: copy.prompt })

    fireEvent.change(field, { target: { value: 'provider migration' } })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    await expect(pending).resolves.toBe('provider migration')
  })

  it('resolves null when the dialog is cancelled', async () => {
    render(<ConfirmHost />)

    const pending = askCronPauseReason(copy)

    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))

    await expect(pending).resolves.toBeNull()
  })

  it('does not submit a blank reason', async () => {
    render(<ConfirmHost />)

    let settled = false
    const pending = askCronPauseReason(copy).then(value => {
      settled = true
      return value
    })

    const dialog = await screen.findByRole('dialog')
    const confirm = screen.getByRole('button', { name: 'Pause' })

    expect(confirm).toHaveProperty('disabled', true)
    fireEvent.keyDown(dialog, { key: 'Enter' })
    expect(settled).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    await expect(pending).resolves.toBeNull()
  })
})
