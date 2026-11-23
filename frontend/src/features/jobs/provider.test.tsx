import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { META } from '../../test/fixtures';
import { Usage } from './JobDetailPage';
import { parseRepoManifest } from './Manifest';
import { ProviderNotice } from './NewJobPage';

const REMOTE = { name: 'openai_compatible', model: 'llama-3.1-8b-instant', remote: true, host: 'api.groq.com', estimated_cost: true };

describe('ProviderNotice', () => {
  it('says plainly, before upload, that text goes to the named provider and model', () => {
    render(<ProviderNotice provider={REMOTE} />);
    const notice = screen.getByRole('note');
    expect(notice).toHaveTextContent('Document text is sent to an external AI provider.');
    expect(notice).toHaveTextContent('api.groq.com');
    expect(notice).toHaveTextContent('llama-3.1-8b-instant');
  });

  it('says nothing leaves the machine with the stub', () => {
    render(<ProviderNotice provider={META.provider} />);
    expect(screen.queryByRole('note')).toBeNull();
    expect(screen.getByText(/No document text leaves/)).toBeVisible();
  });

  it('shows a hostile model name as inert text', () => {
    render(<ProviderNotice provider={{ ...REMOTE, model: '<img src=x onerror=alert(1)>‮' }} />);
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getByRole('note')).toHaveTextContent('\\u202E');
  });
});

describe('Usage', () => {
  it('shows calls, tokens and the estimated cost per provider and model', () => {
    render(
      <Usage
        usage={[
          { provider: 'openai_compatible', model: 'old-model', calls: 2, failed_calls: 1, prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, estimated_cost: null, estimated_cost_currency: null },
          { provider: 'openai_compatible', model: 'llama-3.1-8b-instant', calls: 12, failed_calls: 0, prompt_tokens: 14400, completion_tokens: 3600, total_tokens: 18000, estimated_cost: '0.001008', estimated_cost_currency: 'USD' },
        ]}
      />,
    );
    const [first, second] = screen.getAllByRole('row').slice(1) as [HTMLElement, HTMLElement];
    expect(within(first).getByText('(1 failed)')).toBeVisible();
    expect(second).toHaveTextContent('openai_compatible / llama-3.1-8b-instant');
    expect(second).toHaveTextContent((14400).toLocaleString());
    expect(second).toHaveTextContent('0.001008 USD');
    expect(first).not.toHaveTextContent('USD');
  });

  it('never invents tokens the provider did not report', () => {
    render(
      <Usage
        usage={[{ provider: 'stub', model: 'stub-extractive-v1', calls: 3, failed_calls: 0, prompt_tokens: null, completion_tokens: null, total_tokens: null, estimated_cost: null, estimated_cost_currency: null }]}
      />,
    );
    expect(screen.getAllByText('—')).toHaveLength(4);
  });

  it('explains when nothing was called yet', () => {
    render(<Usage usage={[]} />);
    expect(screen.getByText('No summarization calls yet.')).toBeVisible();
  });
});

describe('parseRepoManifest', () => {
  it('reads the secret-scrubbing count, and null for the stub', () => {
    expect(parseRepoManifest(JSON.stringify({ secret_scrubbing: { replaced: 3, by_kind: { token: 3 } } })).secretsReplaced).toBe(3);
    expect(parseRepoManifest(JSON.stringify({ secret_scrubbing: null })).secretsReplaced).toBeNull();
    expect(parseRepoManifest(JSON.stringify({ secret_scrubbing: { replaced: 'x' } })).secretsReplaced).toBe(0);
  });
});
