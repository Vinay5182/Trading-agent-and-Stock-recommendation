export const CONFIRMATION_TIME_UNAVAILABLE = "Confirmation time unavailable";

export function parseTimestamp(value) {
  if (value === undefined || value === null || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    const date = new Date(value > 10000000000 ? value : value * 1000);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const text = String(value).trim();
  if (!text) return null;
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  const normalized = hasTimezone ? text : `${text}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatIstTimestamp(value) {
  const date = parseTimestamp(value);
  if (!date) return null;
  const parts = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).formatToParts(date).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
  return `${parts.day} ${parts.month} ${parts.year}, ${parts.hour}:${parts.minute} ${String(parts.dayPeriod || "").toUpperCase()} IST`;
}

export function confirmationTimestampValue(row, mode) {
  if (!row) return null;
  if (mode === "swing") return row.swing_confirmed_at || row.confirmed_at || null;
  if (mode === "momentum") return row.momentum_confirmed_at || row.confirmed_at || null;
  return row.latest_confirmed_at || row.confirmed_at || row.swing_confirmed_at || row.momentum_confirmed_at || null;
}

export function confirmationTimestampLabel(row, mode) {
  return formatIstTimestamp(confirmationTimestampValue(row, mode)) || CONFIRMATION_TIME_UNAVAILABLE;
}
