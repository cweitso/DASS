import type { Task, TaskStatus } from "../types"

/** How often to re-check something that is still moving. */
export const LIVE_REFETCH_INTERVAL_MS = 3000

const TERMINAL_STATUSES: TaskStatus[] = ["success", "final_failed"]

export function isTaskSettled(task: Task | undefined): boolean {
  return task !== undefined && TERMINAL_STATUSES.includes(task.status)
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
