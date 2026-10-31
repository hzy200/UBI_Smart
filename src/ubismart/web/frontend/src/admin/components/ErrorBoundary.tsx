import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Icon } from './Icons';

/**
 * React 错误边界。
 *
 * 为什么必须有：没有它时，子树里任何一次渲染抛错都会把**整棵树**卸载，
 * 用户看到的是纯白页面 —— 既不报错也无从下手，是最难排查的失败形态。
 * 有了边界，崩溃被限制在那一块，外壳与导航仍然可用，用户至少能切到别的页
 * 或看到错误原因。
 *
 * 边界只捕获**渲染 / 生命周期 / 构造函数**里的错误。事件处理器与异步回调
 * （fetch 失败等）不会触发它 —— 那些要在调用处自己 try/catch。
 */

type Props = {
  children: ReactNode;
  /** 边界名，进提示文案与日志，便于定位是哪棵子树炸了。 */
  label?: string;
  onError?: (error: Error, info: ErrorInfo) => void;
};

type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  // 不能用 setState 记录：错误阶段它不可靠，必须用这个生命周期。
  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(
      `[admin:${this.props.label ?? 'root'}] 渲染失败`,
      error,
      info.componentStack,
    );
    this.props.onError?.(error, info);
  }

  private reset = () => {
    // 只是清掉错误状态再渲染一次；state 里的坏数据由子树自己重建。
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const label = this.props.label ? `「${this.props.label}」` : '这一块';
    return (
      <div className="boot boot-error" role="alert">
        <Icon name="warning" />
        <div className="boot-error-body">
          <strong>{label}出错了</strong>
          <span className="boot-error-msg">{error.message || String(error)}</span>
          <div className="boot-error-actions">
            <button className="btn" onClick={this.reset}>
              重试
            </button>
            <button className="btn ghost" onClick={() => window.location.reload()}>
              重新加载
            </button>
          </div>
        </div>
      </div>
    );
  }
}
