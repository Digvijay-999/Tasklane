import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';

/** Render inside a fresh query client and an in-memory router (no real network, no URL bar). */
export function renderPage(element: ReactNode, path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const router = createMemoryRouter(
    [
      { path, element },
      { path: '/jobs/:jobId', element: <p>job page</p> },
      { path: '*', element: <p>other page</p> },
    ],
    { initialEntries: [path] },
  );
  return {
    client,
    router,
    ...render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    ),
  };
}
