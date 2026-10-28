import { currentRuntimeConfig } from '../runtime-config';

export function moneySymbol(): string {
  return currentRuntimeConfig()?.cost?.currency === 'USD' ? '$' : '¥';
}

export function formatUsdAsMoney(usd: unknown, decimals = 4): string {
  const cost = currentRuntimeConfig()?.cost;
  const rate = cost?.currency === 'USD' ? 1 : (cost?.usdToCny || 7.2);
  const value = Number(usd || 0) * rate;
  return `${moneySymbol()}${value.toFixed(decimals)}`;
}
