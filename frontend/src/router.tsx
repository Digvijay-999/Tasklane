import { createBrowserRouter } from 'react-router';
import { AppShell, NotFoundPage } from './components/AppShell';
import { OverviewPage } from './features/dashboard/OverviewPage';
import { JobDetailPage } from './features/jobs/JobDetailPage';
import { JobsPage } from './features/jobs/JobsPage';
import { NewJobPage } from './features/jobs/NewJobPage';

export const routes = [
  {
    element: <AppShell />,
    children: [
      { path: '/', element: <OverviewPage /> },
      { path: '/jobs', element: <JobsPage /> },
      { path: '/jobs/new', element: <NewJobPage /> },
      { path: '/jobs/:jobId', element: <JobDetailPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];

export const router = createBrowserRouter(routes);
