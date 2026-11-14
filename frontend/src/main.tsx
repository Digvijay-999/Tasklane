import '@fontsource-variable/ibm-plex-sans';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import './styles/tokens.css';
import './styles/app.css';

import { CSPProvider } from '@base-ui/react/csp-provider';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router';
import { router } from './router';

const queryClient = new QueryClient({
  defaultOptions: {
    // A failed refresh keeps the last data (marked stale by the page), and refetching continues.
    queries: { retry: 1, refetchOnWindowFocus: true },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Base UI must not inject <style> tags: the production CSP allows only same-origin styles. */}
    <CSPProvider disableStyleElements>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </CSPProvider>
  </StrictMode>,
);
