import { ApiError } from './client';
import type { ErrorBody, Upload } from './types';

export interface UploadHandle {
  promise: Promise<Upload>;
  abort: () => void;
}

/**
 * POST /v1/uploads with the raw file as the body. XMLHttpRequest, not fetch: fetch has no upload
 * progress events. `onProgress` receives bytes sent and the total.
 */
export function uploadFile(
  file: File,
  contentType: string,
  onProgress: (sent: number, total: number) => void,
): UploadHandle {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<Upload>((resolve, reject) => {
    xhr.open('POST', `/v1/uploads?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader('Content-Type', contentType);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : file.size);
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // no JSON: treated as unreachable below
      }
      if (xhr.status >= 200 && xhr.status < 300 && body) resolve(body as Upload);
      else {
        const error = (body as ErrorBody | null)?.error;
        reject(new ApiError(xhr.status, error && typeof error.code === 'string' ? error : null));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, null));
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}
