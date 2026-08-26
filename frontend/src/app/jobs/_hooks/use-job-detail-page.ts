"use client"

import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import { useRouter } from "next/navigation"

import { api } from "../../../api/client"
import { isTaskSettled, refetchWhileActive } from "../../../api/polling"
import { useToast } from "../../../hooks/use-toast"
import type { Job } from "../../../types"

export function useJobDetailPage(jobId: string) {
  const router = useRouter()
  const queryClient = useQueryClient()
  const { push: pushToast } = useToast()

  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId),
  })

  const tasksQuery = useQuery({
    queryKey: ["job-tasks", jobId],
    queryFn: () => api.listJobTasks(jobId),
    // Poll while any run is still pending or running, then go quiet.
    refetchInterval: query =>
      refetchWhileActive(
        (query.state.data ?? []).some(task => !isTaskSettled(task))
      ),
  })

  const relatedJobIds = jobQuery.data
    ? Array.from(
        new Set([
          ...jobQuery.data.upstream_job_ids,
          ...jobQuery.data.downstream_job_ids,
        ])
      ).filter(relatedJobId => relatedJobId !== jobId)
    : []

  const relatedJobQueries = useQueries({
    queries: relatedJobIds.map(relatedJobId => ({
      queryKey: ["job", relatedJobId],
      queryFn: () => api.getJob(relatedJobId),
      enabled: Boolean(relatedJobId),
      staleTime: 5 * 60 * 1000,
    })),
  })

  const triggerMutation = useMutation({
    mutationFn: () => api.triggerJob(jobId),
    onSuccess: result => {
      pushToast({
        title: "Job triggered",
        description: `Task ${result.task_id} is now ${result.status}.`,
        tone: "success",
      })
      void queryClient.invalidateQueries({ queryKey: ["job-tasks", jobId] })
    },
    onError: error => {
      pushToast({
        title: "Trigger failed",
        description:
          error instanceof Error ? error.message : "Unable to trigger the job.",
        tone: "error",
      })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteJob(jobId),
    onSuccess: () => {
      pushToast({
        title: "Job deleted",
        description: "The job was removed successfully.",
        tone: "success",
      })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
      router.push("/jobs")
    },
    onError: error => {
      pushToast({
        title: "Delete failed",
        description:
          error instanceof Error ? error.message : "Unable to delete the job.",
        tone: "error",
      })
    },
  })

  return {
    deleteMutation,
    job: jobQuery.data,
    jobQuery,
    relatedJobs: relatedJobQueries
      .map(query => query.data)
      .filter((job): job is Job => Boolean(job)),
    relatedJobsError: relatedJobQueries.some(query => query.isError),
    relatedJobsFetching: relatedJobQueries.some(query => query.isFetching),
    tasks: tasksQuery.data ?? [],
    tasksQuery,
    triggerMutation,
  }
}
