import { useMemo, useState } from "react";
import { fmtDur, fmtHM, hhmm, projectColor, CATEGORY_COLOR } from "./api";
import type { Block, EvidencePoint, AgentLane, Interval } from "./api";

interface Props {
  blocks: Block[];
  away: Interval[];
  unknown: Interval[];
  agent: AgentLane[];
  evidence: EvidencePoint[];
  onBlockClick: (b: Block) => void;
  onUnknownClick?: (iv: Interval) => void;
}

const LANE = { human: 34, away: 14, unknown: 14, agentRow: 18, agentGap: 4, ev: 16 };
const W = 1000; // viewBox width; x = hour/24 * W

function x(h: number) {
  return (h / 24) * W;
}

function projectFill(project: string, isCategory: boolean) {
  return isCategory ? CATEGORY_COLOR : projectColor(project);
}

const CATS = ["终端·其他", "网页浏览", "文献阅读", "文本编辑", "文件管理", "个人事务", "系统", "PyCharm"];

export function isCat(p: string) {
  return CATS.includes(p);
}

export function DayTimeline({ blocks, away, unknown, agent, evidence, onBlockClick, onUnknownClick }: Props) {
  const [hover, setHover] = useState<
    { x: number; y: number; lines: string[] } | null
  >(null);

  // group agent lanes: stack overlapping sessions into rows
  const agentRows = useMemo(() => {
    const sorted = [...agent].sort((a, b) => a.start.localeCompare(b.start));
    const rows: AgentLane[][] = [];
    for (const s of sorted) {
      let placed = false;
      for (const row of rows) {
        if (row[row.length - 1].end <= s.start) {
          row.push(s);
          placed = true;
          break;
        }
      }
      if (!placed) rows.push([s]);
    }
    return rows;
  }, [agent]);

  const agentTop = LANE.human + LANE.away + LANE.unknown + LANE.ev + 8;
  const H = agentTop + rowsH(agentRows) + 24;
  const commitEv = evidence.filter((e) => e.kind === "commit");

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full select-none"
        onMouseLeave={() => setHover(null)}>
        {/* hour grid */}
        {Array.from({ length: 25 }, (_, i) => (
          <g key={i}>
            <line x1={x(i)} x2={x(i)} y1={0} y2={H - 20}
              stroke="#e5e5e5" strokeWidth={i % 6 === 0 ? 1.5 : 0.7} />
            {i % 2 === 0 && (
              <text x={x(i)} y={H - 6} fontSize="10" fill="#737373"
                textAnchor={i === 24 ? "end" : i === 0 ? "start" : "middle"}>
                {String(i).padStart(2, "0")}:00
              </text>
            )}
          </g>
        ))}

        {/* lane labels */}
        <text x={4} y={22} fontSize="11" fill="#525252">人</text>
        <text x={4} y={LANE.human + 10} fontSize="10" fill="#a3a3a3">离开</text>
        <text x={4} y={LANE.human + LANE.away + 10} fontSize="10" fill="#a3a3a3">未知</text>
        <text x={4} y={LANE.human + LANE.away + LANE.unknown + 10} fontSize="10" fill="#a3a3a3">事件</text>

        {/* human blocks */}
        {blocks.map((b) => {
          const x0 = x(hhmm(b.start)), x1 = Math.max(x(hhmm(b.end)), x0 + 0.6);
          return (
            <rect key={b.id} x={x0} y={6} width={x1 - x0} height={LANE.human - 12}
              rx={1.5}
              fill={projectFill(b.project, isCat(b.project))}
              opacity={b.is_manual_override ? 1 : 0.82}
              stroke={b.is_manual_override ? "#111" : "none"}
              strokeWidth={0.8}
              className="cursor-pointer"
              onClick={() => onBlockClick(b)}
              onMouseMove={(e) => {
                const svg = (e.target as SVGElement).ownerSVGElement;
                if (!svg) return;
                const r = svg.getBoundingClientRect();
                setHover({
                  x: (e.clientX - r.left) / r.width * 100,
                  y: 0,
                  lines: [
                    `${fmtHM(b.start)}–${fmtHM(b.end)} (${fmtDur((+new Date(b.end) - +new Date(b.start)) / 1000)})`,
                    `${b.project} · ${b.activity_type}`,
                    b.titles[0] ? `“${b.titles[0].slice(0, 50)}”` : "",
                    `证据 ${b.evidence_count} · 置信 ${Math.round(b.confidence * 100)}%${b.is_manual_override ? " · 人工修正" : ""}`,
                  ].filter(Boolean),
                });
              }}
            />
          );
        })}

        {/* away */}
        {away.map((iv, i) => (
          <rect key={i} x={x(hhmm(iv.start))} y={LANE.human + 2}
            width={Math.max(x(hhmm(iv.end)) - x(hhmm(iv.start)), 0.5)}
            height={LANE.away - 4} fill="#d4d4d4" />
        ))}

        {/* unknown (hatched, clickable) */}
        {unknown.map((iv, i) => (
          <rect key={i} x={x(hhmm(iv.start))} y={LANE.human + LANE.away + 2}
            width={Math.max(x(hhmm(iv.end)) - x(hhmm(iv.start)), 0.5)}
            height={LANE.unknown - 4} fill="url(#hatch)"
            className={onUnknownClick ? "cursor-pointer" : ""}
            onClick={() => onUnknownClick?.(iv)}
            onMouseMove={(e) => {
              const svg = (e.target as SVGElement).ownerSVGElement;
              if (!svg) return;
              const r = svg.getBoundingClientRect();
              setHover({
                x: (e.clientX - r.left) / r.width * 100, y: 0,
                lines: [`未知区间 ${fmtHM(iv.start)}–${fmtHM(iv.end)}`, "点击可标注"],
              });
            }} />
        ))}

        {/* evidence dots */}
        {commitEv.map((e) => (
          <circle key={e.id} cx={x(hhmm(e.at))} cy={LANE.human + LANE.away + LANE.unknown + 8}
            r={3} fill="#111" fillOpacity={0.65} />
        ))}

        {/* agent lanes */}
        {agentRows.map((row, ri) => (
          <g key={ri}>
            {row.map((s) => {
              const x0 = x(hhmm(s.start)), x1 = Math.max(x(hhmm(s.end)), x0 + 1);
              const fill = s.agent === "pi" ? "#0891b2" : "#ea580c";
              return (
                <rect key={s.id} x={x0}
                  y={agentTop + ri * (LANE.agentRow + LANE.agentGap)}
                  width={x1 - x0} height={LANE.agentRow - 2} rx={2}
                  fill={fill} opacity={0.75}
                  onMouseMove={(e) => {
                    const svg = (e.target as SVGElement).ownerSVGElement;
                    if (!svg) return;
                    const r = svg.getBoundingClientRect();
                    setHover({
                      x: (e.clientX - r.left) / r.width * 100, y: 0,
                      lines: [
                        `${s.agent} · ${s.project}`,
                        `${fmtHM(s.start)}–${fmtHM(s.end)} (${fmtDur((+new Date(s.end) - +new Date(s.start)) / 1000)})`,
                        s.cwd ? s.cwd.split("/").slice(-1)[0] : "",
                      ].filter(Boolean),
                    });
                  }} />
              );
            })}
          </g>
        ))}

        <defs>
          <pattern id="hatch" width="5" height="5" patternTransform="rotate(45)"
            patternUnits="userSpaceOnUse">
            <line x1="0" y1="0" x2="0" y2="5" stroke="#f59e0b" strokeWidth="2" />
          </pattern>
        </defs>
      </svg>

      {hover && (
        <div className="pointer-events-none absolute z-10 -translate-x-1/2"
          style={{ left: `${Math.min(Math.max(hover.x, 8), 92)}%`, top: 8 }}
        >
          <div className="rounded bg-neutral-900 px-2 py-1.5 text-[11px] leading-snug text-white shadow-lg">
            {hover.lines.map((l, i) => (
              <div key={i} className={i === 0 ? "font-medium" : "opacity-80"}>{l}</div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function rowsH(rows: AgentLane[][]) {
  return rows.length * (LANE.agentRow + LANE.agentGap);
}
