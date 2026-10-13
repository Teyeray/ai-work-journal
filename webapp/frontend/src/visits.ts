import { appName, cleanTitle, fmtDur } from "./api";
import type { Timeline, WindowVisit } from "./api";

/** Shared time helpers + window-visit model (second precision). */

/** minutes since local midnight (timestamps carry the data tz offset) */
export function t2m(iso: string): number {
  return +iso.slice(11, 13) * 60 + +iso.slice(14, 16) + +iso.slice(17, 19) / 60;
}

/** Interval edge within `day`: an end on a later date (e.g. exactly midnight)
 *  is 1440, a start on an earlier date is 0. */
export function laneM(iso: string, day: string, edge: "start" | "end"): number {
  if (iso.slice(0, 10) === day) return t2m(iso);
  return edge === "start" ? 0 : 1440;
}
export const startM = (iso: string, day: string) => laneM(iso, day, "start");
export const endM = (iso: string, day: string) => laneM(iso, day, "end");

export function hm(m: number): string {
  const r = Math.round(m);
  return `${String(Math.floor(r / 60)).padStart(2, "0")}:${String(r % 60).padStart(2, "0")}`;
}

export function hms(m: number): string {
  const sec = Math.round(m * 60);
  const h = Math.floor(sec / 3600), mi = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(mi).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Duration in minutes → "45s" / "3m05s" / "1h20m". */
export function durS(min: number): string {
  const s = Math.round(min * 60);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60 ? String(s % 60).padStart(2, "0") + "s" : ""}`;
  return fmtDur(s);
}

export interface Visit extends WindowVisit {
  i: number;     // index into tl.windows
  s: number;     // minutes since midnight
  e: number;
  appLabel: string;
}

export function dayVisits(tl: Timeline): Visit[] {
  return tl.windows.map((w, i) => ({
    ...w, i, s: startM(w.start, tl.day_key), e: endM(w.end, tl.day_key), appLabel: appName(w.app),
  }));
}

/** Short "where": the window title, or the project when the title is a shell/tmux name. */
export function placeOf(v: WindowVisit): string {
  const t = cleanTitle(v.title);
  return t.length <= 6 || /^t \S+$/.test(v.title) ? v.project : t;
}
