export function QuotaBar({
  label,
  used,
  limit,
  format,
}: {
  label: string;
  used: number;
  limit: number;
  format: (value: number) => string;
}) {
  const unlimited = !limit || limit <= 0;
  const ratio = unlimited ? 0 : Math.min(1, Math.max(0, used / limit));
  const percent = Math.round(ratio * 100);
  const tone = percent >= 100 ? 'bad' : percent >= 80 ? 'warn' : 'ok';
  return (
    <div className="quota">
      <div className="quota-head">
        <span>{label}</span>
        <span className={unlimited ? 'muted' : ''}>
          {unlimited ? '不限制' : `${format(used)} / ${format(limit)}`}
        </span>
      </div>
      {!unlimited && (
        <>
          <progress className={`quota-progress ${tone}`} max={100} value={percent} />
          <div className="quota-foot">
            <span className={`pill ${tone === 'ok' ? 'ok' : tone === 'warn' ? 'warn' : 'bad'}`}>
              已用 {percent}%
            </span>
            <span className="muted">剩余 {format(Math.max(0, limit - used))}</span>
          </div>
        </>
      )}
    </div>
  );
}
