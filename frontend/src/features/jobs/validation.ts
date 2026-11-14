import type { WorkflowMeta } from '../../lib/api/types';
import { formatBytes } from '../../lib/format';

// The same filename rules as the server (backend/app/domain/uploads.py: filename_problem), so a
// name the server would refuse is caught before uploading anything.
const REJECTED = /[\p{Cc}\p{Zl}\p{Zp}]/u;
const BIDI_CONTROLS = /[؜‎‏‪-‮⁦-⁩]/u;

/** Checks that need only the name and size. Returns a message, or null when acceptable. */
export function fileProblem(
  file: { name: string; size: number },
  workflow: WorkflowMeta,
  maxFilenameLength: number,
): string | null {
  const kind = workflow.upload_kind.toUpperCase();
  const extension = file.name.includes('.') ? file.name.slice(file.name.lastIndexOf('.')).toLowerCase() : '';
  if (!workflow.extensions.includes(extension)) {
    return `This workflow needs a ${kind} file (${workflow.extensions.join(', ')}).`;
  }
  if (file.size === 0) return 'The file is empty.';
  if (file.size > workflow.max_upload_bytes) {
    return `This file is ${formatBytes(file.size)}; ${kind} uploads are limited to ${formatBytes(workflow.max_upload_bytes)}.`;
  }
  if (!file.name.trim()) return 'The filename is empty.';
  if ([...file.name].length > maxFilenameLength) {
    return `The filename is longer than ${maxFilenameLength} characters. Rename the file and choose it again.`;
  }
  if (REJECTED.test(file.name)) return 'The filename contains control characters or line breaks. Rename the file.';
  if (BIDI_CONTROLS.test(file.name)) {
    return 'The filename contains invisible text-direction characters, which can disguise its real type. Rename the file.';
  }
  return null;
}

/** The file must start with the workflow's signature bytes (%PDF-, PK\x03\x04). */
export async function signatureProblem(file: Blob, workflow: WorkflowMeta): Promise<string | null> {
  const expected = workflow.signature_hex.match(/../g)!.map((pair) => parseInt(pair, 16));
  const head = new Uint8Array(await file.slice(0, expected.length).arrayBuffer());
  const matches = expected.every((byte, index) => head[index] === byte);
  return matches
    ? null
    : `The file's contents don't look like a ${workflow.upload_kind.toUpperCase()}, even though its name ends in ${workflow.extensions.join(', ')}.`;
}
