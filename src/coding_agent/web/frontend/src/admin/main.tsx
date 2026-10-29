import { createRoot } from 'react-dom/client';
import { App } from './App';
import { ErrorBoundary } from './components/ErrorBoundary';

const container = document.getElementById('root');
if (container) {
  // 最外层也要有边界：否则 `App` 自己在渲染阶段抛错就是一整屏白。
  createRoot(container).render(
    <ErrorBoundary label="控制台">
      <App />
    </ErrorBoundary>,
  );
}
