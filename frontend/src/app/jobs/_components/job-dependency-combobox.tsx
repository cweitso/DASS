"use client"

import { useMemo } from "react"

import type { Job } from "../../../types"

// Page size for the candidate lookup. The list is filtered server-side by the
// search box, so this caps one page of results rather than the jobs you can pick.
export const JOB_DEPENDENCY_COMBOBOX_PAGE_SIZE = 50

type JobDependencyComboboxProps = {
  description: string
  error?: string
  hasMore?: boolean
  isLoading?: boolean
  label: string
  loadingLabel?: string
  onChange: (selectedIds: string[]) => void
  onQueryChange: (query: string) => void
  options: Job[]
  query: string
  currentJobId?: string
  selectedIds: string[]
  selectedJobs: Job[]
}

export function JobDependencyCombobox({
  description,
  error,
  hasMore = false,
  isLoading,
  label,
  loadingLabel = "Loading jobs...",
  onChange,
  onQueryChange,
  options,
  query,
  currentJobId,
  selectedIds,
  selectedJobs,
}: JobDependencyComboboxProps) {
  // The server has already applied the search; only drop what cannot be picked.
  const availableJobs = useMemo(
    () =>
      options
        .filter(job => job.id !== currentJobId && !selectedIds.includes(job.id))
        .sort((left, right) => left.name.localeCompare(right.name)),
    [currentJobId, options, selectedIds]
  )

  const addJob = (jobId: string) => {
    onChange([...selectedIds, jobId])
    onQueryChange("")
  }

  const removeJob = (jobId: string) => {
    onChange(selectedIds.filter(id => id !== jobId))
  }

  return (
    <section className="space-y-4 rounded-3xl border border-line bg-panel-strong/50 p-5">
      <div>
        <h3 className="text-sm font-semibold uppercase tracking-[0.24em] text-muted">
          {label}
        </h3>
        <p className="mt-2 text-sm text-muted">{description}</p>
      </div>

      <div className="space-y-3">
        <label className="flex flex-col gap-2 text-sm text-muted">
          <span>Selected jobs</span>
          <div className="rounded-2xl border border-line bg-panel px-3 py-3">
            {selectedIds.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {selectedIds.map(id => {
                  const job = selectedJobs.find(
                    candidate => candidate.id === id
                  )
                  return (
                    <div
                      className="flex items-center gap-2 rounded-full border border-line bg-panel-strong px-3 py-1.5 text-xs text-fg"
                      key={id}
                    >
                      <span className="font-medium">
                        {job?.name ?? "Loading..."}
                      </span>
                      <span className="font-mono text-muted">{id}</span>
                      <button
                        className="text-muted transition hover:text-danger"
                        onClick={() => removeJob(id)}
                        type="button"
                      >
                        Remove
                      </button>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-xs text-muted">No jobs selected yet.</p>
            )}
          </div>
        </label>

        <label className="flex flex-col gap-2 text-sm text-muted">
          <span>Search jobs</span>
          <input
            className="rounded-2xl border border-line bg-panel px-4 py-2.5 font-medium text-fg outline-none transition placeholder:text-muted/45 focus:border-accent/50"
            onChange={event => onQueryChange(event.target.value)}
            placeholder="Search by job name"
            value={query}
          />
        </label>
      </div>

      <div className="rounded-2xl border border-line bg-panel px-3 py-3">
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="text-xs uppercase tracking-[0.24em] text-muted">
            Available jobs
          </p>
          <p className="text-xs text-muted">{availableJobs.length} results</p>
        </div>
        <div className="max-h-64 space-y-2 overflow-auto pr-1">
          {isLoading ? (
            <p className="px-1 py-3 text-sm text-muted">{loadingLabel}</p>
          ) : availableJobs.length > 0 ? (
            availableJobs.map(job => (
              <button
                className="flex w-full flex-col gap-1 rounded-2xl border border-line bg-panel-strong px-4 py-3 text-left transition hover:border-accent/40 hover:bg-panel"
                key={job.id}
                onClick={() => addJob(job.id)}
                type="button"
              >
                <span className="text-sm font-semibold text-fg">
                  {job.name}
                </span>
                <span className="font-mono text-xs text-muted">{job.id}</span>
              </button>
            ))
          ) : (
            <p className="px-1 py-3 text-sm text-muted">
              {query
                ? "No matching jobs found."
                : "No jobs available to select."}
            </p>
          )}
        </div>
        {hasMore ? (
          <p className="mt-2 px-1 text-xs text-muted">
            More jobs match than are shown — narrow the search to find them.
          </p>
        ) : null}
      </div>

      {error ? <p className="text-xs text-danger">{error}</p> : null}
    </section>
  )
}
