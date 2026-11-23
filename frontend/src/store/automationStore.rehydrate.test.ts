import { describe, it, expect } from 'vitest';
import { normalizeRehydratedTriggers } from './automationStore';
import type { Trigger } from '../types/automation';

/** Minimal shape factory — only fields the normalizer touches. */
function mk(partial: Partial<Trigger>): Trigger {
  return {
    id: 't1',
    symbol: 'SPY',
    type: 'stopLoss',
    status: 'armed',
    enabled: true,
    condition: { operator: 'lte', threshold: 100 },
    action: { side: 'sell', quantityMode: 'fixed', quantity: 1 },
    repeat: false,
    createdAt: 0,
    updatedAt: 0,
    triggerCount: 0,
    successCount: 0,
    failCount: 0,
    ...partial,
  } as Trigger;
}

describe('normalizeRehydratedTriggers', () => {
  it('revives a "triggered" trigger stranded mid-execution by a page close', () => {
    // The 'triggered' reservation is set synchronously before the async
    // execute; a reload lands AFTER the reservation but BEFORE the terminal
    // write, so the persisted status is 'triggered'. processTriggers only
    // evaluates armed/blocked — without revival the stop-loss stays dead.
    const revived = normalizeRehydratedTriggers([
      mk({ id: 'dead', status: 'triggered', repeat: true }),
    ]);
    expect(revived[0].status).toBe('armed');
    expect(revived[0].statusReason).toContain('rehydrate');
  });

  it('demotes a non-repeat "triggered" trigger to failed (one-shot already consumed)', () => {
    const revived = normalizeRehydratedTriggers([
      mk({ status: 'triggered', repeat: false }),
    ]);
    expect(revived[0].status).toBe('failed');
  });

  it('leaves every other status untouched', () => {
    const statuses = ['armed', 'paused', 'blocked', 'completed', 'failed', 'cancelled', 'expired'] as const;
    const revived = normalizeRehydratedTriggers(statuses.map((s, i) => mk({ id: `t${i}`, status: s })));
    expect(revived.map((t) => t.status)).toEqual([...statuses]);
  });

  it('updates updatedAt when reviving', () => {
    const before = Date.now() - 60_000;
    const revived = normalizeRehydratedTriggers([
      mk({ status: 'triggered', repeat: true, updatedAt: before }),
    ]);
    expect(revived[0].updatedAt).toBeGreaterThanOrEqual(before);
  });
});
