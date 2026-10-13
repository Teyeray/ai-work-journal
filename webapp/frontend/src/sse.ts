import { useEffect, useRef, useState } from "react";

/**
 * One shared EventSource per page for /api/v1/events.
 *
 * Every useSse() subscribes to the same connection: browsers allow only 6
 * HTTP/1.1 connections per host, and one long-lived stream per component
 * (×2 tabs, or during a navigation while the old page's streams close)
 * used to exhaust them and stall every other request.
 */
type Msg = { revision: number; affected_days: string[] };
const subs = new Set<(m: Msg) => void>();
const statusSubs = new Set<(c: boolean) => void>();
let es: EventSource | null = null;
let connected = false;

function ensure() {
  if (es) return;
  es = new EventSource("/api/v1/events");
  es.onopen = () => { connected = true; statusSubs.forEach((f) => f(true)); };
  es.onerror = () => { connected = false; statusSubs.forEach((f) => f(false)); };
  es.addEventListener("data_changed", (e) => {
    let msg: Msg;
    try { msg = JSON.parse((e as MessageEvent).data); } catch { return; }
    subs.forEach((f) => { try { f(msg); } catch { /* one bad subscriber must not stop the rest */ } });
  });
}

function release() {
  if (es && subs.size === 0 && statusSubs.size === 0) { es.close(); es = null; connected = false; }
}

if (typeof window !== "undefined") window.addEventListener("pagehide", () => { es?.close(); es = null; });

/** Subscribe to data_changed; returns whether the stream is connected. */
export function useSse(onChange: (msg: Msg) => void) {
  const cbRef = useRef(onChange);
  cbRef.current = onChange;
  const [ok, setOk] = useState(connected);

  useEffect(() => {
    const f = (m: Msg) => cbRef.current(m);
    subs.add(f);
    statusSubs.add(setOk);
    ensure();
    setOk(connected);
    return () => { subs.delete(f); statusSubs.delete(setOk); release(); };
  }, []);

  return ok;
}
