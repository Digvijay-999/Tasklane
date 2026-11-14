import { useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { ErrorNotice, ProgressBar, Skeleton } from '../../components/ui';
import { ApiError, api, type CreateJobResult } from '../../lib/api/client';
import type { WorkflowMeta, WorkflowType } from '../../lib/api/types';
import { uploadFile } from '../../lib/api/upload';
import { formatBytes, percent } from '../../lib/format';
import { useMeta } from '../../lib/hooks';
import { WORKFLOW_DESCRIPTION, WORKFLOW_LABEL } from '../../lib/labels';
import { fileProblem, signatureProblem } from './validation';

type Fault = { mode: 'off' } | { mode: 'transient'; failures: number } | { mode: 'permanent' };

type Phase =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'uploading'; sent: number; total: number }
  | { kind: 'creating' }
  | { kind: 'failed'; step: 'upload' | 'create'; error: unknown }
  | { kind: 'replayed'; result: CreateJobResult };

/**
 * One submission = one Idempotency-Key. If creating the job fails without a clear answer
 * (network error, 5xx), "Try again" sends the same key with the same upload, so the server
 * returns the job it may already have created instead of making a second one.
 */
interface Submission {
  fingerprint: string;
  key: string;
  uploadId: string | null;
}

const fingerprintOf = (workflow: WorkflowType, file: File, fault: Fault) =>
  JSON.stringify([workflow, file.name, file.size, file.lastModified, fault]);

const retryable = (error: unknown) => error instanceof ApiError && (error.unreachable || error.status >= 500);

export function NewJobPage() {
  const meta = useMeta();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const ids = { file: useId(), fileError: useId(), fileHint: useId(), failures: useId() };

  const [workflow, setWorkflow] = useState<WorkflowType>('pdf_ingest');
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [fault, setFault] = useState<Fault>({ mode: 'off' });
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });
  const submission = useRef<Submission | null>(null);
  const abortUpload = useRef<(() => void) | null>(null);
  const [dragging, setDragging] = useState(false);

  const workflowMeta: WorkflowMeta | undefined = meta.data?.workflows.find((w) => w.workflow_type === workflow);
  const busy = phase.kind === 'checking' || phase.kind === 'uploading' || phase.kind === 'creating';

  const choose = (next: File | null, forWorkflow = workflow) => {
    setFile(next);
    setPhase({ kind: 'idle' });
    const wf = meta.data?.workflows.find((w) => w.workflow_type === forWorkflow);
    setProblem(next && wf && meta.data ? fileProblem(next, wf, meta.data.max_filename_length) : null);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file || !workflowMeta || !meta.data || busy) return;
    const sizeProblem = fileProblem(file, workflowMeta, meta.data.max_filename_length);
    if (sizeProblem) return setProblem(sizeProblem);
    setPhase({ kind: 'checking' });
    const contentProblem = await signatureProblem(file, workflowMeta);
    if (contentProblem) {
      setProblem(contentProblem);
      return setPhase({ kind: 'idle' });
    }

    const fingerprint = fingerprintOf(workflow, file, fault);
    if (submission.current?.fingerprint !== fingerprint) {
      submission.current = { fingerprint, key: crypto.randomUUID(), uploadId: null };
    }
    const current = submission.current;

    if (current.uploadId === null) {
      setPhase({ kind: 'uploading', sent: 0, total: file.size });
      const handle = uploadFile(file, workflowMeta.upload_content_type, (sent, total) =>
        setPhase({ kind: 'uploading', sent, total }),
      );
      abortUpload.current = handle.abort;
      try {
        current.uploadId = (await handle.promise).id;
      } catch (error) {
        abortUpload.current = null;
        if (error instanceof DOMException && error.name === 'AbortError') return setPhase({ kind: 'idle' });
        return setPhase({ kind: 'failed', step: 'upload', error });
      }
      abortUpload.current = null;
    }

    setPhase({ kind: 'creating' });
    try {
      const options = fault.mode === 'off' ? undefined : { fault_injection: fault.mode === 'transient' ? fault : { mode: 'permanent' } };
      const result = await api.createJob(
        { workflow_type: workflow, upload_id: current.uploadId!, ...(options ? { options } : {}) },
        current.key,
      );
      void queryClient.invalidateQueries({ queryKey: ['jobs'] });
      void queryClient.invalidateQueries({ queryKey: ['overview'] });
      if (result.replayed) {
        setPhase({ kind: 'replayed', result });
      } else {
        submission.current = null;
        void navigate(`/jobs/${result.job.id}`);
      }
    } catch (error) {
      // Only an unanswered request (network error, 5xx) is retried with the same key; any other
      // answer means the server stored nothing for it, so the next submission starts fresh.
      if (!retryable(error)) submission.current = null;
      setPhase({ kind: 'failed', step: 'create', error });
    }
  };

  if (meta.isPending) {
    return (
      <>
        <h1>New job</h1>
        <div className="form-skeleton" aria-busy="true">
          <Skeleton height={56} />
          <Skeleton height={56} />
          <Skeleton height={36} width={220} />
        </div>
      </>
    );
  }
  if (meta.isError || !meta.data) {
    return (
      <>
        <h1>New job</h1>
        <ErrorNotice
          error={meta.error}
          action={
            <button type="button" className="button" data-variant="secondary" onClick={() => void meta.refetch()}>
              Load the upload limits again
            </button>
          }
        />
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1>New job</h1>
      </div>
      <form className="form" onSubmit={(e) => void submit(e)} noValidate>
        <fieldset className="field" disabled={busy}>
          <legend>Workflow</legend>
          <div className="choices">
            {meta.data.workflows.map((wf) => (
              <label key={wf.workflow_type} className="choice">
                <input
                  type="radio"
                  name="workflow"
                  value={wf.workflow_type}
                  checked={workflow === wf.workflow_type}
                  onChange={() => {
                    setWorkflow(wf.workflow_type);
                    choose(file, wf.workflow_type);
                  }}
                />
                <span className="choice-text">
                  <span className="choice-title">{WORKFLOW_LABEL[wf.workflow_type] ?? wf.workflow_type}</span>
                  <span className="muted">{WORKFLOW_DESCRIPTION[wf.workflow_type]}</span>
                  <span className="muted num">
                    {wf.extensions.join(', ')} up to {formatBytes(wf.max_upload_bytes)}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="field">
          <label htmlFor={ids.file} className="field-label">
            File
          </label>
          <div
            className="dropzone"
            data-dragging={dragging || undefined}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              if (!busy) choose(e.dataTransfer.files[0] ?? null);
            }}
          >
            <input
              id={ids.file}
              type="file"
              accept={workflowMeta?.extensions.join(',')}
              disabled={busy}
              aria-invalid={problem ? true : undefined}
              aria-describedby={`${ids.fileHint}${problem ? ` ${ids.fileError}` : ''}`}
              onChange={(e) => choose(e.target.files?.[0] ?? null)}
            />
            <span id={ids.fileHint} className="muted">
              {/* The file control already shows the name; add what it doesn't: the size. */}
              {file ? (
                <span className="num">{formatBytes(file.size)}</span>
              ) : (
                `Choose a ${workflowMeta?.upload_kind.toUpperCase()} file or drop it here.`
              )}
            </span>
          </div>
          {problem && (
            <p id={ids.fileError} className="field-error" role="alert">
              {problem}
            </p>
          )}
        </div>

        {meta.data.fault_injection_enabled && (
          <fieldset className="field" disabled={busy}>
            <legend>Fault injection (development only)</legend>
            <p className="muted">The server has fault injection on. It makes the summarizer fail on purpose, to watch retries.</p>
            <div className="choices choices-inline">
              <label className="choice">
                <input type="radio" name="fault" checked={fault.mode === 'off'} onChange={() => setFault({ mode: 'off' })} />
                <span>Off</span>
              </label>
              <label className="choice">
                <input
                  type="radio"
                  name="fault"
                  checked={fault.mode === 'transient'}
                  onChange={() => setFault({ mode: 'transient', failures: 1 })}
                />
                <span>Fail the first attempts (transient)</span>
              </label>
              <label className="choice">
                <input type="radio" name="fault" checked={fault.mode === 'permanent'} onChange={() => setFault({ mode: 'permanent' })} />
                <span>Fail permanently</span>
              </label>
            </div>
            {fault.mode === 'transient' && (
              <label className="inline-field" htmlFor={ids.failures}>
                Failing attempts
                <input
                  id={ids.failures}
                  type="number"
                  min={1}
                  max={meta.data.max_fault_failures}
                  value={fault.failures}
                  onChange={(e) =>
                    setFault({
                      mode: 'transient',
                      failures: Math.max(1, Math.min(meta.data.max_fault_failures, Number(e.target.value) || 1)),
                    })
                  }
                />
              </label>
            )}
          </fieldset>
        )}

        <div className="form-actions">
          <button type="submit" className="button" data-variant="primary" disabled={!file || !!problem || busy}>
            {phase.kind === 'failed' && retryable(phase.error) ? 'Try again' : 'Upload and start job'}
          </button>
          {phase.kind === 'uploading' && (
            <button type="button" className="button" data-variant="secondary" onClick={() => abortUpload.current?.()}>
              Cancel upload
            </button>
          )}
        </div>

        <div className="submit-status" aria-live="polite">
          {phase.kind === 'checking' && <p className="muted">Checking the file…</p>}
          {phase.kind === 'uploading' && (
            <div className="upload-progress">
              <span>Uploading</span>
              <ProgressBar value={(phase.sent / Math.max(1, phase.total)) * 100} label="Upload progress" tone="running" />
              <span className="num">
                {percent((phase.sent / Math.max(1, phase.total)) * 100)} ({formatBytes(phase.sent)} of {formatBytes(phase.total)})
              </span>
            </div>
          )}
          {phase.kind === 'creating' && <p className="muted">Upload complete. Creating the job…</p>}
          {phase.kind === 'failed' && (
            <ErrorNotice
              error={phase.error}
              action={
                <p className="muted">
                  {phase.step === 'upload'
                    ? 'The file was not uploaded.'
                    : retryable(phase.error)
                      ? 'The job request may not have reached the server. Try again sends the same request with the same idempotency key, so it cannot create a second job.'
                      : 'No job was created.'}
                </p>
              }
            />
          )}
          {phase.kind === 'replayed' && (
            <div className="notice" data-tone="info" role="status">
              <p>
                The server already had this submission, so it returned the existing job instead of creating a second one
                (Idempotent-Replayed).
              </p>
              <Link to={`/jobs/${phase.result.job.id}`}>Open the existing job</Link>
            </div>
          )}
        </div>
      </form>
    </>
  );
}
