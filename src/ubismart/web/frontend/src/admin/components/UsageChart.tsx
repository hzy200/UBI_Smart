import type { UsageDayPoint } from '../lib/usage';
import { formatCost, formatNumber } from '../lib/format';

type Metric = 'cost' | 'tokens';

const WIDTH = 640;
const HEIGHT = 180;
const PAD_X = 42;
const PAD_Y = 22;

export function UsageChart({
  points,
  metric,
  title,
  locale = 'zh-CN',
}: {
  points: UsageDayPoint[];
  metric: Metric;
  title: string;
  locale?: string;
}) {
  if (!points.length) return null;
  const values = points.map((point) =>
    metric === 'cost' ? point.cost_cny : point.tokens,
  );
  const max = Math.max(...values, metric === 'cost' ? 0.0001 : 1);
  const innerWidth = WIDTH - PAD_X * 2;
  const innerHeight = HEIGHT - PAD_Y * 2;
  const step = innerWidth / points.length;
  const barWidth = Math.max(2, Math.min(28, step * 0.6));
  const labelEvery = Math.max(1, Math.ceil(points.length / 7));
  const peak = metric === 'cost' ? formatCost(max) : formatNumber(max, locale);
  const label = locale === 'en-US'
    ? `${title}: ${points.length} days, peak ${peak}`
    : `${title}：${points.length} 天，峰值 ${peak}`;

  return (
    <div className="chart-card">
      <h3>{title}</h3>
      <svg
        className="chart-svg"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={label}
      >
        <line
          className="chart-axis"
          x1={PAD_X}
          y1={PAD_Y + innerHeight}
          x2={WIDTH - PAD_X}
          y2={PAD_Y + innerHeight}
        />
        <text
          className="chart-label"
          x={PAD_X - 6}
          y={PAD_Y + 4}
          textAnchor="end"
        >
          {peak}
        </text>
        {points.map((point, index) => {
          const value =
            metric === 'cost' ? point.cost_cny : point.tokens;
          const height = Math.max(0, (value / max) * innerHeight);
          const x = PAD_X + step * index + (step - barWidth) / 2;
          const y = PAD_Y + innerHeight - height;
          const formatted =
            metric === 'cost' ? formatCost(value) : formatNumber(value, locale);
          return (
            <g key={point.day}>
              <rect
                className={`chart-bar ${metric}`}
                x={x}
                y={y}
                width={barWidth}
                height={height}
                rx={2}
              >
                <title>
                  {locale === 'en-US'
                    ? `${point.day} · ${formatted} · ${point.runs} runs`
                    : `${point.day} · ${formatted} · ${point.runs} 次运行`}
                </title>
              </rect>
              {index % labelEvery === 0 && (
                <text
                  className="chart-label"
                  x={PAD_X + step * index + step / 2}
                  y={HEIGHT - 6}
                  textAnchor="middle"
                >
                  {point.day.slice(5)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
