import { useEffect, useState } from "react";
import { api, fmtHM } from "./api";
import type { SessionRow, SessionDetail, Review } from "./api";
import { useSse } from "./sse";

export function Sessions({ day }: { day: string }) {
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [sel, setSel] = useState<number | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [onlyDay, setOnlyDay] = useState(true);
  const [filter, setFilter] = useState("");

  const load = () => {
    const params: Record<string, string | number> = { limit: 100 };
    if (onlyDay) params.day = day;
    if (filter) params.query = filter;
    api.sessions(params).then((r) => setRows(r.sessions)).catch(() => {});
  };
  useEffect(() => { load(); }, [day, onlyDay, filter]);
  useSse(load);

  useEffect(() => {
    if (sel == null) { setDetail(null); return; }
    api.session(sel).then(setDetail).catch(() => setDetail(null));
  }, [sel]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-semibold">会话浏览器</h2>
        <label className="ml-auto flex items-center gap-1.5 text-sm">
          <input type="checkbox" checked={onlyDay} onChange={(e) => setOnlyDay(e.target.checked)} />
          仅 {day}
        </label>
        <input value={filter} onChange={(e) => setFilter(e.target.value)}
          className="input w-56" placeholder="搜索会话内容…" />
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <div className="card max-h-[70vh] overflow-auto p-0 lg:col-span-2">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-white">
              <tr className="border-b text-left text-neutral-500">
                <th className="p-2">时间</th><th className="p-2">Agent</th>
                <th className="p-2">项目</th><th className="p-2">状态</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id} className={`cursor-pointer border-b hover:bg-neutral-50 ${sel === s.id ? "bg-blue-50" : ""}`}
                  onClick={() => setSel(s.id)}>
                  <td className="p-2 whitespace-nowrap">{fmtHM(s.start_at)}</td>
                  <td className="p-2"><span className={s.agent === "pi" ? "text-cyan-700" : "text-orange-600"}>{s.agent}</span></td>
                  <td className="p-2 truncate max-w-32">{s.project}</td>
                  <td className="p-2">
                    {s.status === "running"
                      ? <span className="badge bg-green-100 text-green-700">运行中</span>
                      : <span className="text-neutral-400">{s.n_events}ev</span>}
                  </td>
                </tr>
              ))}
              {!rows.length && (
                <tr><td colSpan={4} className="p-6 text-center text-neutral-400">无会话</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="lg:col-span-3">
          {detail ? <SessionPane detail={detail} /> :
            <div className="card p-8 text-center text-sm text-neutral-400">
              选择左侧会话查看完整对话与评审
            </div>}
        </div>
      </div>
    </div>
  );
}

const RUBRIC: { key: keyof Review; label: string; max: number }[] = [
  { key: "prompt_goal", label: "目标明确", max: 25 },
  { key: "prompt_context", label: "上下文充分", max: 25 },
  { key: "prompt_constraints", label: "约束清晰", max: 25 },
  { key: "prompt_acceptance", label: "验收标准", max: 25 },
  { key: "exec_planning", label: "规划", max: 15 },
  { key: "exec_tools", label: "工具使用", max: 20 },
  { key: "exec_error_recovery", label: "错误恢复", max: 20 },
  { key: "exec_verification", label: "验证", max: 30 },
  { key: "exec_efficiency", label: "效率", max: 15 },
];

function SessionPane({ detail }: { detail: SessionDetail }) {
  const [review, setReview] = useState<Review | null>(
    detail.review && detail.review.status === "analyzed" ? (detail.review as unknown as Review) : null,
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [rating, setRating] = useState("");
  const [comment, setComment] = useState("");

  const userMsgs = detail.events.filter((e) => e.role === "user" || e.text?.trim());

  return (
    <div className="space-y-3">
      <div className="card p-4">
        <div className="flex items-center gap-2">
          <span className={`badge ${detail.agent === "pi" ? "bg-cyan-100 text-cyan-700" : "bg-orange-100 text-orange-700"}`}>
            {detail.agent}
          </span>
          <h3 className="font-semibold">{detail.project}</h3>
          <span className="text-xs text-neutral-500 truncate max-w-64">{detail.cwd}</span>
          <span className="ml-auto text-xs text-neutral-500">
            {fmtHM(detail.start_at)}–{fmtHM(detail.end_at ?? detail.start_at)} · {detail.events.length} 事件
          </span>
        </div>
      </div>

      <div className="card max-h-64 overflow-auto p-4">
        <div className="mb-2 text-xs font-medium text-neutral-600">对话摘要(首条输入等)</div>
        {userMsgs.slice(0, 30).map((e) => (
          <div key={e.id} className="mb-1.5 text-xs">
            <span className="text-neutral-400">{fmtHM(e.at)} </span>
            <span className={e.role === "user" ? "font-medium text-neutral-800" : "text-neutral-500"}>
              {(e.text || "").slice(0, 200)}
            </span>
            {e.tools?.length ? <span className="ml-1 text-cyan-600">[{e.tools.join(",")}]</span> : null}
          </div>
        ))}
        {!userMsgs.length && <div className="text-xs text-neutral-400">无人工输入(自动会话)</div>}
      </div>

      {/* review */}
      <div className="card p-4">
        <div className="mb-2 flex items-center gap-2">
          <h3 className="font-medium">质量评审</h3>
          <button className="btn-primary ml-auto" disabled={busy}
            onClick={async () => {
              setBusy(true); setErr(null);
              try {
                const r = await api.review(detail.id);
                if (r.status === "llm_disabled") {
                  setErr(`LLM 未启用(${r.message ?? ""});可在 webapp/config.json 打开`);
                } else if (r.review) setReview(r.review);
                else setErr(r.message ?? "评审失败");
              } catch (e) { setErr(String(e)); }
              setBusy(false);
            }}>{review ? "重新评审" : "开始评审"}</button>
        </div>

        {busy && <div className="text-xs text-neutral-400">分析中(pi headless,约 10–60s)…</div>}
        {err && <div className="text-xs text-red-600">{err}</div>}

        {review ? (
          <div className="space-y-3">
            <div className="grid grid-cols-3 gap-2 text-xs md:grid-cols-5">
              {RUBRIC.map(({ key, label, max }) => {
                const v = (review as unknown as Record<string, number | undefined>)[key];
                return (
                  <div key={key} className="rounded border p-2">
                    <div className="text-neutral-500">{label}</div>
                    <div className="mt-0.5 font-semibold">
                      {v != null ? `${v}/${max}` : <span className="text-neutral-300">—</span>}
                    </div>
                  </div>
                );
              })}
            </div>
            {review.summary && (
              <div className="rounded bg-neutral-100 p-2 text-xs leading-relaxed">{review.summary}</div>
            )}
            {review.suggestions?.length ? (
              <div>
                <div className="mb-1 text-xs font-medium">改进建议</div>
                <ul className="list-decimal space-y-1 pl-5 text-xs">
                  {review.suggestions.map((s, i) => <li key={i}>{s}</li>)}
                </ul>
              </div>
            ) : null}
            <div className="flex items-end gap-2 border-t pt-2">
              <select value={rating} onChange={(e) => setRating(e.target.value)} className="input w-32 text-xs">
                <option value="">评价建议…</option>
                <option value="helpful">有帮助</option>
                <option value="partially">部分有用</option>
                <option value="not_helpful">没帮助</option>
                <option value="wrong">不准确</option>
              </select>
              <input value={comment} onChange={(e) => setComment(e.target.value)}
                className="input flex-1 text-xs" placeholder="一句话说明(可选)" />
              <button className="btn" disabled={!rating}
                onClick={async () => {
                  await api.feedback({ session_id: detail.id, rating, comment });
                  setRating(""); setComment("");
                }}>提交反馈</button>
            </div>
          </div>
        ) : (
          <div className="text-xs text-neutral-400">
            评审由本地 pi headless 运行(默认关闭)。评分维度:提示 4×25 + 执行 5 项。
          </div>
        )}
      </div>
    </div>
  );
}
