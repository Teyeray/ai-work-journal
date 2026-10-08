import { useEffect, useState } from "react";
import { api } from "./api";
import type { Project } from "./api";
import { useSse } from "./sse";

export function Projects() {
  const [rows, setRows] = useState<Project[]>([]);
  const [editing, setEditing] = useState<number | null>(null);
  const [goals, setGoals] = useState("");
  const [priority, setPriority] = useState(3);

  const load = () => api.projects().then((r) => setRows(r.projects)).catch(() => {});
  useEffect(() => { load(); }, []);
  useSse(load);

  const save = async (p: Project) => {
    await api.patchProject(p.id, { goals_json: goals || null, priority });
    setEditing(null);
    load();
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-semibold">项目</h2>
        <span className="text-xs text-neutral-500">
          优先级与目标为用户所有;重跑采集不会覆盖
        </span>
      </div>

      <div className="card p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-neutral-500">
              <th className="p-3">项目</th><th className="p-3">优先级</th>
              <th className="p-3">目标</th><th className="p-3">更新于</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.id} className="border-b last:border-0">
                <td className="p-3 font-medium">{p.name}</td>
                <td className="p-3">
                  {editing === p.id ? (
                    <select value={priority} onChange={(e) => setPriority(+e.target.value)}
                      className="input w-20 text-xs">
                      {[1, 2, 3, 4, 5].map((v) => <option key={v} value={v}>{v}</option>)}
                    </select>
                  ) : (
                    <span className={p.priority <= 2 ? "font-semibold text-red-600" : "text-neutral-500"}>
                      P{p.priority}
                    </span>
                  )}
                </td>
                <td className="p-3 text-xs text-neutral-600">
                  {editing === p.id ? (
                    <input value={goals} onChange={(e) => setGoals(e.target.value)}
                      className="input w-full text-xs" placeholder="当前目标…" />
                  ) : (p.goals_json || <span className="text-neutral-300">—</span>)}
                </td>
                <td className="p-3 text-right text-xs">
                  {editing === p.id ? (
                    <span className="flex justify-end gap-2">
                      <button className="btn" onClick={() => setEditing(null)}>取消</button>
                      <button className="btn-primary" onClick={() => save(p)}>保存</button>
                    </span>
                  ) : (
                    <button className="link" onClick={() => {
                      setEditing(p.id); setGoals(p.goals_json ?? ""); setPriority(p.priority);
                    }}>编辑</button>
                  )}
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan={4} className="p-8 text-center text-neutral-400">
                尚无项目(首次访问时间轴后自动生成)</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
