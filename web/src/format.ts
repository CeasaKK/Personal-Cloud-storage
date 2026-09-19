export function bytes(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  let v = n;
  while (Math.abs(v) >= 1000 && i < units.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}

const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
const monthFmt = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export const dayLabel = (ts: number) => dayFmt.format(new Date(ts * 1000));
export const monthLabel = (ts: number) => monthFmt.format(new Date(ts * 1000));
export const dateTime = (ts: number | null | undefined) => (ts ? fullFmt.format(new Date(ts * 1000)) : "—");

export function ago(ts: number | null | undefined): string {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function duration(s: number | null): string {
  if (!s) return "";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

export const dayKey = (ts: number) => {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
};
