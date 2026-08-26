"use client"

import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import Link from "next/link"
import { useRouter, useSearchParams } from "next/navigation"
import { useEffect, useMemo, useState } from "react"
import type { FormEvent } from "react"

import { api } from "../../../api/client"
import { useDebouncedValue } from "../../../hooks/use-debounced-value"
import { useToast } from "../../../hooks/use-toast"
import type { ActionType, ConcurrencyPolicy, Job } from "../../../types"
import { normalizeCronExpression } from "../_lib/job-form.utils"
import {
  JOB_DEPENDENCY_COMBOBOX_PAGE_SIZE,
  JobDependencyCombobox,
} from "./job-dependency-combobox"

type JobFormState = {
  name: string
  cron_expression: string
  action_type: ActionType
  enabled: boolean
  concurrency_policy: ConcurrencyPolicy
  max_retries: string
  upstream_job_ids: string[]
  http: {
    method: string
    url: string
    headers: string
    body: string
    timeout_seconds: string
  }
  shell: {
    command: string
    timeout_seconds: string
  }
}

type JobFormErrors = Partial<Record<string, string>>

const defaultHttpState = {
  method: "GET",
  url: "",
  headers: "{}",
  body: "",
  timeout_seconds: "30",
}

const defaultShellState = {
  command: "echo hello from dass",
  timeout_seconds: "30",
}

function createDefaultFormState(): JobFormState {
  return {
    name: "",
    cron_expression: "*/5 * * * *",
    action_type: "http",
    enabled: true,
    concurrency_policy: "allow",
    max_retries: "0",
    upstream_job_ids: [],
    http: { ...defaultHttpState },
    shell: { ...defaultShellState },
  }
}

function formatJson(value: unknown) {
  if (value === undefined) {
    return ""
  }

  if (typeof value === "string") {
    return value
  }

  return JSON.stringify(value, null, 2)
}

function toFormState(job: Job): JobFormState {
  const actionConfig = job.action_config ?? {}

  return {
    name: job.name ?? "",
    cron_expression: job.cron_expression ?? "",
    action_type: job.action_type,
    enabled: job.enabled,
    concurrency_policy: job.concurrency_policy,
    max_retries: String(job.max_retries ?? 0),
    upstream_job_ids: job.upstream_job_ids ?? [],
    http: {
      method: String(actionConfig.method ?? "GET"),
      url: String(actionConfig.url ?? ""),
      headers: formatJson(actionConfig.headers ?? {}),
      body: formatJson(actionConfig.body ?? ""),
      timeout_seconds: String(actionConfig.timeout_seconds ?? 30),
    },
    shell: {
      command: String(actionConfig.command ?? ""),
      timeout_seconds: String(actionConfig.timeout_seconds ?? 30),
    },
  }
}

function parseJsonObject(
  value: string,
  fieldLabel: string,
  errors: JobFormErrors
) {
  const trimmed = value.trim()

  if (!trimmed) {
    return {}
  }

  try {
    const parsed = JSON.parse(trimmed)
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      errors[fieldLabel] = "Must be a JSON object."
      return null
    }
    return parsed as Record<string, unknown>
  } catch {
    errors[fieldLabel] = "Must be valid JSON."
    return null
  }
}

function parseBody(value: string, errors: JobFormErrors) {
  const trimmed = value.trim()
  if (!trimmed) {
    return undefined
  }

  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) {
    return value
  }

  try {
    return JSON.parse(trimmed) as Record<string, unknown> | unknown[]
  } catch {
    errors.action_body = "Body must be valid JSON or plain text."
    return null
  }
}

function validateForm(form: JobFormState, currentJobId?: string) {
  const errors: JobFormErrors = {}

  if (!form.name.trim()) {
    errors.name = "Job name is required."
  }

  const maxRetries = Number.parseInt(form.max_retries, 10)
  if (!Number.isInteger(maxRetries) || maxRetries < 0) {
    errors.max_retries = "Max retries must be a non-negative integer."
  }

  if (currentJobId && form.upstream_job_ids.includes(currentJobId)) {
    errors.upstream_job_ids = "A job cannot depend on itself."
    return errors
  }

  if (form.action_type === "http") {
    if (!form.http.url.trim()) {
      errors.http_url = "URL is required for HTTP jobs."
    }

    if (!form.http.method.trim()) {
      errors.http_method = "Method is required."
    }

    const timeoutSeconds = Number.parseInt(form.http.timeout_seconds, 10)
    if (!Number.isInteger(timeoutSeconds) || timeoutSeconds <= 0) {
      errors.http_timeout_seconds = "Timeout must be a positive integer."
    }

    const headers = parseJsonObject(form.http.headers, "http_headers", errors)
    if (headers === null) {
      return errors
    }

    if (parseBody(form.http.body, errors) === null) {
      return errors
    }
  } else {
    if (!form.shell.command.trim()) {
      errors.shell_command = "Command is required for shell jobs."
    }

    const timeoutSeconds = Number.parseInt(form.shell.timeout_seconds, 10)
    if (!Number.isInteger(timeoutSeconds) || timeoutSeconds <= 0) {
      errors.shell_timeout_seconds = "Timeout must be a positive integer."
    }
  }

  return errors
}

function ErrorText({ children }: { children?: string }) {
  if (!children) {
    return null
  }

  return <p className="text-xs text-danger">{children}</p>
}

export default function JobFormPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const jobId = searchParams.get("jobId") ?? ""
  const isEditing = Boolean(jobId)
  const queryClient = useQueryClient()
  const { push: pushToast } = useToast()

  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId),
    enabled: isEditing,
  })

  // Candidate search runs on the server. Filtering a single fetched page in the
  // browser silently hid every job past the first page from the picker.
  const [dependencyQuery, setDependencyQuery] = useState("")
  const debouncedDependencyQuery = useDebouncedValue(dependencyQuery, 250)

  const jobsQuery = useQuery({
    queryKey: ["jobs", "dependency-picker", debouncedDependencyQuery],
    queryFn: () =>
      api.listJobs({
        page: 1,
        page_size: JOB_DEPENDENCY_COMBOBOX_PAGE_SIZE,
        q: debouncedDependencyQuery || undefined,
      }),
    placeholderData: previous => previous,
  })

  const [form, setForm] = useState<JobFormState>(() => createDefaultFormState())

  // Selected upstreams are resolved by id so a dependency that falls outside the
  // current search results still renders as a named chip instead of vanishing.
  const selectedUpstreamJobs = useQueries({
    queries: form.upstream_job_ids.map(id => ({
      queryKey: ["job", id],
      queryFn: () => api.getJob(id),
      staleTime: 5 * 60 * 1000,
    })),
    combine: results =>
      results
        .map(result => result.data)
        .filter((job): job is Job => Boolean(job)),
  })
  const [errors, setErrors] = useState<JobFormErrors>({})
  const [submitError, setSubmitError] = useState("")

  useEffect(() => {
    if (jobQuery.data) {
      setForm(toFormState(jobQuery.data))
    }
  }, [jobQuery.data])

  const mutation = useMutation({
    mutationFn: async () => {
      const validationErrors = validateForm(form, jobId || undefined)
      if (Object.keys(validationErrors).length > 0) {
        setErrors(validationErrors)
        throw new Error("Please fix the highlighted fields.")
      }

      const maxRetries = Number.parseInt(form.max_retries, 10)
      const cronExpression = normalizeCronExpression(form.cron_expression)
      const upstreamJobIds = form.upstream_job_ids

      if (form.action_type === "http") {
        const actionErrors: JobFormErrors = {}
        const headers = parseJsonObject(
          form.http.headers,
          "http_headers",
          actionErrors
        )
        const body = parseBody(form.http.body, actionErrors)
        if (
          Object.keys(actionErrors).length > 0 ||
          headers === null ||
          body === null
        ) {
          setErrors(current => ({ ...current, ...actionErrors }))
          throw new Error("Please fix the highlighted fields.")
        }

        const payload = {
          name: form.name.trim(),
          cron_expression: cronExpression,
          action_type: form.action_type,
          action_config: {
            method: form.http.method.trim() || "GET",
            url: form.http.url.trim(),
            headers,
            body,
            timeout_seconds: Number.parseInt(form.http.timeout_seconds, 10),
          },
          enabled: form.enabled,
          concurrency_policy: form.concurrency_policy,
          max_retries: Number.isFinite(maxRetries) ? maxRetries : 0,
          upstream_job_ids: upstreamJobIds,
        }

        return isEditing
          ? api.updateJob(jobId, payload)
          : api.createJob(payload)
      }

      const payload = {
        name: form.name.trim(),
        cron_expression: normalizeCronExpression(form.cron_expression),
        action_type: form.action_type,
        action_config: {
          command: form.shell.command.trim(),
          timeout_seconds: Number.parseInt(form.shell.timeout_seconds, 10),
        },
        enabled: form.enabled,
        concurrency_policy: form.concurrency_policy,
        max_retries: Number.isFinite(maxRetries) ? maxRetries : 0,
        upstream_job_ids: upstreamJobIds,
      }

      return isEditing ? api.updateJob(jobId, payload) : api.createJob(payload)
    },
    onSuccess: job => {
      pushToast({
        title: isEditing ? "Job updated" : "Job created",
        description: `"${job.name}" was saved successfully.`,
        tone: "success",
      })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
      void queryClient.invalidateQueries({ queryKey: ["job", job.id] })
      router.push(`/jobs/${job.id}`)
    },
    onError: error => {
      if (
        error instanceof Error &&
        error.message === "Please fix the highlighted fields."
      ) {
        return
      }

      const message =
        error instanceof Error ? error.message : "Unable to save the job."
      setSubmitError(message)
      pushToast({
        title: "Save failed",
        description: message,
        tone: "error",
      })
    },
  })

  const actionTitle = useMemo(
    () => (isEditing ? "Edit Job" : "Create Job"),
    [isEditing]
  )
  const presetTextClass = isEditing ? "text-fg" : "text-accent/90 font-medium"
  const strongFieldBaseClass =
    "rounded-2xl border border-line bg-panel-strong px-4 py-2.5 outline-none transition placeholder:text-muted/45 focus:border-accent/50"
  const panelFieldBaseClass =
    "rounded-2xl border border-line bg-panel px-4 py-2.5 outline-none transition placeholder:text-muted/45 focus:border-accent/50"
  const panelTextareaBaseClass =
    "min-h-32 rounded-2xl border border-line bg-panel px-4 py-3 outline-none transition placeholder:text-muted/45 focus:border-accent/50"

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSubmitError("")
    setErrors({})
    mutation.mutate()
  }

  if (isEditing && jobQuery.isLoading) {
    return (
      <div className="rounded-3xl border border-line bg-panel p-8 shadow-glow backdrop-blur-sm">
        <p className="text-sm text-muted">Loading job...</p>
      </div>
    )
  }

  if (isEditing && (jobQuery.isError || !jobQuery.data)) {
    return (
      <div className="rounded-3xl border border-line bg-panel p-8 shadow-glow backdrop-blur-sm">
        <h2 className="text-lg font-semibold">Could not load job</h2>
        <p className="mt-2 text-sm text-muted">
          {(jobQuery.error as Error)?.message ||
            "The selected job could not be loaded."}
        </p>
        <div className="mt-5 flex flex-wrap gap-3">
          <button
            className="rounded-2xl border border-line bg-panel-strong px-4 py-2 text-sm font-semibold text-fg transition hover:border-accent/40 hover:bg-panel"
            onClick={() => jobQuery.refetch()}
            type="button"
          >
            Retry
          </button>
          <Link
            className="rounded-2xl border border-line bg-panel-strong px-4 py-2 text-sm font-semibold text-fg transition hover:border-accent/40 hover:bg-panel"
            href="/jobs"
          >
            Back to jobs
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-3xl border border-line bg-panel p-6 shadow-glow backdrop-blur-sm sm:p-8">
      <div className="flex flex-col gap-3 border-b border-line pb-6">
        <p className="text-xs uppercase tracking-[0.35em] text-accent">
          Job Form
        </p>
        <div>
          <h2 className="text-3xl font-semibold text-fg">{actionTitle}</h2>
          <p className="mt-2 max-w-3xl text-sm text-muted">
            Define the schedule, upstream dependencies, action payload, and
            retry behavior. Validation runs in the browser before the job is
            submitted to the API.
          </p>
        </div>
      </div>

      <form
        className="mt-6 space-y-6"
        onSubmit={onSubmit}
      >
        <div className="grid gap-4 lg:grid-cols-2">
          <label className="flex flex-col gap-2 text-sm text-muted">
            <span>Job name</span>
            <input
              className={`${strongFieldBaseClass} text-fg`}
              onChange={event =>
                setForm(current => ({ ...current, name: event.target.value }))
              }
              placeholder="Daily report sync"
              value={form.name}
            />
            <ErrorText>{errors.name}</ErrorText>
          </label>

          <label className="flex flex-col gap-2 text-sm text-muted">
            <span>Cron expression</span>
            <input
              className={`${strongFieldBaseClass} font-mono ${presetTextClass}`}
              onChange={event =>
                setForm(current => ({
                  ...current,
                  cron_expression: event.target.value,
                }))
              }
              placeholder="*/5 * * * *"
              value={form.cron_expression}
            />
            <p className="text-xs text-muted">
              Leave this blank for a one-time job.
            </p>
            <ErrorText>{errors.cron_expression}</ErrorText>
          </label>
        </div>

        <div className="grid gap-4 lg:grid-cols-4">
          <label className="flex flex-col gap-2 text-sm text-muted">
            <span>Action type</span>
            <select
              className={`${strongFieldBaseClass} ${presetTextClass}`}
              onChange={event =>
                setForm(current => ({
                  ...current,
                  action_type: event.target.value as ActionType,
                }))
              }
              value={form.action_type}
            >
              <option value="http">HTTP</option>
              <option value="shell">Shell</option>
            </select>
          </label>

          <label className="flex flex-col gap-2 text-sm text-muted">
            <span>Concurrency policy</span>
            <select
              className={`${strongFieldBaseClass} ${presetTextClass}`}
              onChange={event =>
                setForm(current => ({
                  ...current,
                  concurrency_policy: event.target.value as ConcurrencyPolicy,
                }))
              }
              value={form.concurrency_policy}
            >
              <option value="allow">Allow</option>
              <option value="forbid">Forbid</option>
              <option value="replace">Replace</option>
            </select>
          </label>

          <label className="flex flex-col gap-2 text-sm text-muted">
            <span>Max retries</span>
            <input
              className={`${strongFieldBaseClass} ${presetTextClass}`}
              inputMode="numeric"
              min={0}
              onChange={event =>
                setForm(current => ({
                  ...current,
                  max_retries: event.target.value,
                }))
              }
              type="number"
              value={form.max_retries}
            />
            <ErrorText>{errors.max_retries}</ErrorText>
          </label>

          <label className="flex items-center gap-3 rounded-2xl border border-line bg-panel-strong px-4 py-2.5 text-sm text-muted">
            <input
              checked={form.enabled}
              className="h-4 w-4 accent-(--accent)"
              onChange={event =>
                setForm(current => ({
                  ...current,
                  enabled: event.target.checked,
                }))
              }
              type="checkbox"
            />
            <span>Enabled</span>
          </label>
        </div>

        <JobDependencyCombobox
          currentJobId={jobId || undefined}
          description="Pick upstream jobs by name. Their IDs are shown so you can confirm the dependency target before saving."
          error={errors.upstream_job_ids}
          hasMore={
            (jobsQuery.data?.total ?? 0) > JOB_DEPENDENCY_COMBOBOX_PAGE_SIZE
          }
          isLoading={jobsQuery.isLoading}
          label="Upstream dependencies"
          onChange={upstream_job_ids =>
            setForm(current => ({
              ...current,
              upstream_job_ids,
            }))
          }
          onQueryChange={setDependencyQuery}
          options={jobsQuery.data?.items ?? []}
          query={dependencyQuery}
          selectedIds={form.upstream_job_ids}
          selectedJobs={selectedUpstreamJobs}
        />

        {form.action_type === "http" ? (
          <section className="space-y-4 rounded-3xl border border-line bg-panel-strong/50 p-5">
            <div>
              <h3 className="text-sm font-semibold uppercase tracking-[0.24em] text-muted">
                HTTP action
              </h3>
              <p className="mt-2 text-sm text-muted">
                Provide the request details for the job runner.
              </p>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <label className="flex flex-col gap-2 text-sm text-muted">
                <span>Method</span>
                <input
                  className={`${panelFieldBaseClass} font-mono ${presetTextClass}`}
                  onChange={event =>
                    setForm(current => ({
                      ...current,
                      http: { ...current.http, method: event.target.value },
                    }))
                  }
                  placeholder="GET"
                  value={form.http.method}
                />
                <ErrorText>{errors.http_method}</ErrorText>
              </label>

              <label className="flex flex-col gap-2 text-sm text-muted">
                <span>Timeout seconds</span>
                <input
                  className={`${panelFieldBaseClass} ${presetTextClass}`}
                  inputMode="numeric"
                  min={1}
                  onChange={event =>
                    setForm(current => ({
                      ...current,
                      http: {
                        ...current.http,
                        timeout_seconds: event.target.value,
                      },
                    }))
                  }
                  type="number"
                  value={form.http.timeout_seconds}
                />
                <ErrorText>{errors.http_timeout_seconds}</ErrorText>
              </label>
            </div>

            <label className="flex flex-col gap-2 text-sm text-muted">
              <span>URL</span>
              <input
                className={`${panelFieldBaseClass} text-fg`}
                onChange={event =>
                  setForm(current => ({
                    ...current,
                    http: { ...current.http, url: event.target.value },
                  }))
                }
                placeholder="https://example.com/webhook"
                value={form.http.url}
              />
              <ErrorText>{errors.http_url}</ErrorText>
            </label>

            <label className="flex flex-col gap-2 text-sm text-muted">
              <span>Headers JSON</span>
              <textarea
                className={`${panelTextareaBaseClass} font-mono text-sm ${presetTextClass}`}
                onChange={event =>
                  setForm(current => ({
                    ...current,
                    http: { ...current.http, headers: event.target.value },
                  }))
                }
                placeholder={'{ "Authorization": "Bearer ..." }'}
                value={form.http.headers}
              />
              <ErrorText>{errors.http_headers}</ErrorText>
            </label>

            <label className="flex flex-col gap-2 text-sm text-muted">
              <span>Body</span>
              <textarea
                className={`${panelTextareaBaseClass} font-mono text-sm text-fg`}
                onChange={event =>
                  setForm(current => ({
                    ...current,
                    http: { ...current.http, body: event.target.value },
                  }))
                }
                placeholder="Optional raw text or JSON body"
                value={form.http.body}
              />
              <ErrorText>{errors.action_body}</ErrorText>
            </label>
          </section>
        ) : (
          <section className="space-y-4 rounded-3xl border border-line bg-panel-strong/50 p-5">
            <div>
              <h3 className="text-sm font-semibold uppercase tracking-[0.24em] text-muted">
                Shell action
              </h3>
              <p className="mt-2 text-sm text-muted">
                Provide the shell command that should be executed.
              </p>
            </div>

            <label className="flex flex-col gap-2 text-sm text-muted">
              <span>Command</span>
              <textarea
                className={`${panelTextareaBaseClass} font-mono text-sm ${presetTextClass}`}
                onChange={event =>
                  setForm(current => ({
                    ...current,
                    shell: { ...current.shell, command: event.target.value },
                  }))
                }
                placeholder="echo hello"
                value={form.shell.command}
              />
              <ErrorText>{errors.shell_command}</ErrorText>
            </label>

            <label className="flex flex-col gap-2 text-sm text-muted">
              <span>Timeout seconds</span>
              <input
                className={`${panelFieldBaseClass} ${presetTextClass}`}
                inputMode="numeric"
                min={1}
                onChange={event =>
                  setForm(current => ({
                    ...current,
                    shell: {
                      ...current.shell,
                      timeout_seconds: event.target.value,
                    },
                  }))
                }
                type="number"
                value={form.shell.timeout_seconds}
              />
              <ErrorText>{errors.shell_timeout_seconds}</ErrorText>
            </label>
          </section>
        )}

        {submitError ? (
          <div className="rounded-2xl border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
            {submitError}
          </div>
        ) : null}

        <div className="flex flex-wrap gap-3">
          <button
            className="rounded-2xl bg-accent px-5 py-2.5 text-sm font-semibold text-accent-fg transition hover:brightness-105 disabled:cursor-not-allowed disabled:opacity-60"
            disabled={mutation.isPending}
            type="submit"
          >
            {mutation.isPending
              ? isEditing
                ? "Saving..."
                : "Creating..."
              : isEditing
                ? "Save Job"
                : "Create Job"}
          </button>
          <Link
            className="rounded-2xl border border-line bg-panel-strong px-5 py-2.5 text-sm font-semibold text-fg transition hover:border-accent/40 hover:bg-panel"
            href="/jobs"
          >
            Cancel
          </Link>
        </div>
      </form>
    </div>
  )
}
