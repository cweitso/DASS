import type { Task, TaskStatus } from "../types"

/** How often to re-check something that is still moving. */
export const LIVE_REFETCH_INTERVAL_MS = 3000

/**
 * The only statuses a task can still move out of.
 *
 * Defined as the moving set rather than the terminal one because "failed" is
 * easy to misread: it records a single attempt that has already finished, and a
 * retry is a **new** task row with retry_count + 1. Treating it as non-terminal
 * makes a page poll forever as soon as any run has been retried.
 */
const ACTIVE_STATUSES: TaskStatus[] = ["pending", "running"]

export function isTaskSettled(task: Task | undefined): boolean {
  return task !== undefined && !ACTIVE_STATUSES.includes(task.status)
}

/**
 * Poll only while something can still change.
 *
 * A scheduler dashboard that never refetches shows a task as "pending" until the
 * user reloads; one that polls forever keeps hitting the API for rows that will
 * never move again. Both are avoided by driving the interval from the data.
 */
export function refetchWhileActive(hasActiveWork: boolean): number | false {
  return hasActiveWork ? LIVE_REFETCH_INTERVAL_MS : false
}
