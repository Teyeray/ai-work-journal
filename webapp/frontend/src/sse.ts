import { useEffect, useRef, useState } from "react";

/** Subscribe to /api/v1/events SSE; call cb on data_changed messages. */
export function useSse(onChange: (msg: { revision: number; affected_days: string[] }) => void) {
  const cbRef = useRef(onChange);
  cbRef.current = onChange;
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const es = new EventSource("/api/v1/events");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("data_changed", (e) => {
      try {
        cbRef.current(JSON.parse((e as MessageEvent).data));
      } catch {
        /* ignore */
      }
    });
    return () => es.close();
  }, []);

  return connected;
}
