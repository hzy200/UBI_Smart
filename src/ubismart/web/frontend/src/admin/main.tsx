import { createRoot } from 'react-dom/client';
import { App } from './App';
import { ErrorBoundary } from './components/ErrorBoundary';
import { AdminI18nProvider } from './i18n';

const container = document.getElementById('root');
if (container) {
  // 最外层也要有边界：否则 `App` 自己在渲染阶段抛错就是一整屏白。
  createRoot(container).render(
    <AdminI18nProvider>
      <ErrorBoundary label="Admin console">
        <App />
      </ErrorBoundary>
    </AdminI18nProvider>,
  );
}
