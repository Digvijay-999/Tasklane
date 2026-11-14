import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api } from './api/client';
import type { WorkflowMeta, WorkflowType } from './api/types';

/** Limits, accepted types and stages from GET /v1/meta (fixed while the server runs). */
export function useMeta() {
  return useQuery({ queryKey: ['meta'], queryFn: api.meta, staleTime: Infinity, retry: 3 });
}

export function useWorkflowMeta(workflow: WorkflowType | undefined): WorkflowMeta | undefined {
  return useMeta().data?.workflows.find((w) => w.workflow_type === workflow);
}

/** The current time, re-read every `intervalMs` while `active` (for durations that tick). */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}
