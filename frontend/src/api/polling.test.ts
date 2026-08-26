import { describe, expect, it } from "vitest"

import type { Task, TaskStatus } from "../types"
import {
  LIVE_REFETCH_INTERVAL_MS,
  isTaskSettled,
  refetchWhileActive,
} from "./polling"

function task(status: TaskStatus): Task {
  return { id: "t1", job_id: "j1", status } as Task
}

describe("isTaskSettled", () => {
  it("treats pending and running as still moving", () => {
    expect(isTaskSettled(task("pending"))).toBe(false)
    expect(isTaskSettled(task("running"))).toBe(false)
  })

  it("treats every finished status as settled", () => {
    expect(isTaskSettled(task("success"))).toBe(true)
    expect(isTaskSettled(task("final_failed"))).toBe(true)
    // A retry is a separate task row, so this attempt is finished for good.
    // Missing it here made the page poll forever after any retry.
    expect(isTaskSettled(task("failed"))).toBe(true)
  })

  it("treats a task that has not loaded yet as unsettled", () => {
    expect(isTaskSettled(undefined)).toBe(false)
  })
})

describe("refetchWhileActive", () => {
  it("polls while work is in flight and stops when it is not", () => {
    expect(refetchWhileActive(true)).toBe(LIVE_REFETCH_INTERVAL_MS)
    expect(refetchWhileActive(false)).toBe(false)
  })
})
