"use client"

import { useQuery } from "@tanstack/react-query"

import { api } from "../../../api/client"
import { isTaskSettled, refetchWhileActive } from "../../../api/polling"

export function useTaskDetailPage(taskId: string) {
  const taskQuery = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.getTask(taskId),
    // Keep refreshing until the task reaches a terminal state, then stop.
    refetchInterval: query =>
      refetchWhileActive(!isTaskSettled(query.state.data)),
  })

  return {
    task: taskQuery.data,
    taskQuery,
  }
}
