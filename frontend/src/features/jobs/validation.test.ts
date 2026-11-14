import { describe, expect, it } from 'vitest';
import { META } from '../../test/fixtures';
import { fileProblem, signatureProblem } from './validation';

const pdf = META.workflows[0]!;
const zip = META.workflows[1]!;

describe('fileProblem (limits from GET /v1/meta)', () => {
  it('accepts a matching file within the limit', () => {
    expect(fileProblem({ name: 'Report.PDF', size: 1000 }, pdf, 255)).toBeNull();
  });

  it('rejects the wrong extension for the workflow', () => {
    expect(fileProblem({ name: 'repo.zip', size: 10 }, pdf, 255)).toBe('This workflow needs a PDF file (.pdf).');
    expect(fileProblem({ name: 'report.pdf', size: 10 }, zip, 255)).toMatch(/needs a ZIP file/);
  });

  it('uses the limit the server reported, not a constant', () => {
    expect(fileProblem({ name: 'big.pdf', size: 1001 }, pdf, 255)).toBe(
      'This file is 1001 B; PDF uploads are limited to 1000 B.',
    );
    expect(fileProblem({ name: 'big.zip', size: 1500 }, zip, 255)).toBeNull();
  });

  it('rejects empty files, long names, control and hidden direction characters', () => {
    expect(fileProblem({ name: 'a.pdf', size: 0 }, pdf, 255)).toBe('The file is empty.');
    expect(fileProblem({ name: `${'x'.repeat(300)}.pdf`, size: 1 }, pdf, 255)).toMatch(/longer than 255/);
    expect(fileProblem({ name: 'invoice‮gpj.pdf', size: 1 }, pdf, 255)).toMatch(/text-direction/);
    expect(fileProblem({ name: 'a\nb.pdf', size: 1 }, pdf, 255)).toMatch(/control characters/);
  });
});

describe('signatureProblem', () => {
  it('checks the first bytes against the signature', async () => {
    expect(await signatureProblem(new Blob(['%PDF-1.7 ...']), pdf)).toBeNull();
    expect(await signatureProblem(new Blob([new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1])]), zip)).toBeNull();
    expect(await signatureProblem(new Blob(['<html>']), pdf)).toMatch(/look like a PDF/);
  });
});
