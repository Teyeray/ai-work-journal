import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { ScreenStatus } from "./api";
import { tr } from "./i18n";

/** Header warning when screen capture (screenpipe) has stopped. Silent when it
 *  works or is not configured; screenpipe can stall for days without a sign. */

function since(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) { const n = Math.max(1, Math.round(s / 60)); return tr(`${n} 分钟`, `${n} min`); }
  if (s < 86400 * 2) { const n = Math.round(s / 3600); return tr(`${n} 小时`, `${n} h`); }
  { const n = Math.floor(s / 86400); return tr(`${n} 天`, `${n} d`); }
}

export function ScreenAlert() {
  const [st, setSt] = useState<ScreenStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const check = () => api.screenStatus().then(setSt).catch(() => {});
  // every 5 min normally; every 10 s while waiting for you to approve the dialog
  const [kicked, setKicked] = useState(0);   // a restart from this page: poll fast for a while
  const waiting = !!st?.awaiting_approval || Date.now() - kicked < 3 * 60_000;
  useEffect(() => {
    check();
    const t = setInterval(check, waiting ? 10_000 : 5 * 60_000);
    return () => clearInterval(t);
  }, [waiting]); // eslint-disable-line
  const restart = async () => {
    setBusy(true); setErr(null);
    try { await api.screenRestart(); setKicked(Date.now()); } catch (e) { setErr(String(e).replace(/^Error:\s*/, "")); }
    await check();
    setBusy(false);
  };
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  if (!st || st.state === "ok" || st.state === "off") return null;
  const label = st.state === "down" ? tr("截屏未运行", "Screen capture not running")
    : st.awaiting_approval ? tr("截屏等待授权", "Screen capture awaiting approval")
    : tr("截屏已停止", "Screen capture stopped") + (st.last_frame ? " " + since(st.last_frame) : "");
  return (
    <div ref={root} className="relative">
      <button className="screen-alert" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />{label}
      </button>
      {open && (
        <div className="screen-alert-pop" role="dialog" aria-label={tr("截屏状态", "Screen capture status")}>
          <div className="text-[13px] font-semibold text-neutral-900">
            {st.awaiting_approval ? tr("截屏已重启,等你授权", "Screen capture restarted, waiting for your approval") : tr(`屏幕截图(${st.source === "builtin" ? "内置截屏" : "screenpipe"})没有在记录`, `Screen capture (${st.source === "builtin" ? "built-in" : "screenpipe"}) is not recording`)}
          </div>
          {st.last_frame && (
            <div className="mt-1 text-neutral-500">{tr("最后一帧", "Last frame")} {st.last_frame.slice(0, 16).replace("T", " ")}</div>
          )}
          {!!st.monitors?.length && <div className="text-neutral-500">{tr(`识别到 ${st.monitors.length} 个显示器:${st.monitors.join("、")}`, `Detected ${st.monitors.length} ${st.monitors.length === 1 ? "monitor" : "monitors"}: ${st.monitors.join(", ")}`)}</div>}
          {st.ocr && st.ocr.engine !== "screenpipe" && (
            <div className="text-neutral-500">OCR{tr(":", ": ")}{st.ocr.engine}{st.ocr.model ? ` · ${st.ocr.model}` : ""}{st.ocr.problem ? `${tr("(", " (")}${st.ocr.problem}${tr(")", ")")}` : ""}</div>
          )}
          {st.reason && <div className="mt-2 text-neutral-700"><b className="font-medium">{tr("原因:", "Cause: ")}</b>{st.reason}</div>}
          {st.hint && <div className="mt-1 text-neutral-700"><b className="font-medium">{tr("处理:", "Fix: ")}</b>{st.hint}</div>}
          {err && <div className="mt-2 text-red-600">{err}</div>}
          <div className="mt-2.5 flex items-center gap-2">
            {st.restartable && (
              <button className="btn-primary" disabled={busy} onClick={restart}
                title={tr("重启 screenpipe 服务,屏幕共享对话框会重新弹出", "Restart the screenpipe service; the screen-sharing dialog will pop up again")}>
                {busy ? tr("重启中…", "Restarting…") : st.awaiting_approval ? tr("再弹一次授权", "Ask for approval again") : tr("重启截屏", "Restart capture")}
              </button>
            )}
            <button className="btn" onClick={check}>{tr("重新检测", "Check again")}</button>
            <span className="text-[11px] text-neutral-400">{waiting ? tr("每 10 秒检测", "Checking every 10 s") : tr("每 5 分钟自动检测", "Auto-checks every 5 min")}</span>
          </div>
        </div>
      )}
    </div>
  );
}
