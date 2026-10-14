// UI language. Strings live next to the code that shows them: tr("中文", "English").
// The choice is kept in localStorage and mirrored into a `lang` cookie so the
// backend (same origin) writes its generated text — insights, status reasons,
// 问 AI answers — in the same language without every fetch passing it along.
import { useSyncExternalStore } from "react";

export type Lang = "zh" | "en";

function initial(): Lang {
  try {
    const q = new URLSearchParams(location.search).get("lang");
    if (q === "zh" || q === "en") return q;
    const s = localStorage.getItem("lang");
    if (s === "zh" || s === "en") return s;
  } catch { /* storage blocked */ }
  return navigator.language?.toLowerCase().startsWith("zh") ? "zh" : "en";
}

let current: Lang = initial();
const listeners = new Set<() => void>();

function apply(l: Lang) {
  document.documentElement.lang = l === "zh" ? "zh-CN" : "en";
  document.cookie = `lang=${l}; path=/; max-age=31536000; samesite=lax`;
}
apply(current);

export function getLang(): Lang { return current; }

export function setLang(l: Lang) {
  if (l === current) return;
  current = l;
  try { localStorage.setItem("lang", l); } catch { /* storage blocked */ }
  apply(l);
  listeners.forEach((f) => f());
}

export function useLang(): Lang {
  return useSyncExternalStore((f) => { listeners.add(f); return () => listeners.delete(f); }, getLang);
}

/** Pick the string for the current language. */
export function tr(zh: string, en: string): string {
  return current === "en" ? en : zh;
}

/** Locale for Intl / toLocale* formatting. */
export function locale(): string {
  return current === "en" ? "en-US" : "zh-CN";
}

// Labels that come from data (built-in categories, lanes, activity kinds).
// They stay Chinese in the database, where they are keys; only the display changes.
const LABELS: Record<string, string> = {
  "其他活动": "Other activity", "其他应用": "Other apps", "终端·其他": "Terminal · other",
  "网页浏览": "Web browsing", "文献阅读": "Paper reading", "文本编辑": "Text editing",
  "文件管理": "File manager", "个人事务": "Personal", "系统": "System", "通信": "Chat",
  "阅读": "Reading", "其他": "Other", "未知应用": "Unknown app",
  "终端/Agent 交互": "Terminal / agent", "浏览": "Browsing", "编码": "Coding", "写作": "Writing",
  "文件": "Files", "沟通": "Communication",
  "<5 分钟": "<5 min", "5–15 分钟": "5–15 min", "15–30 分钟": "15–30 min",
  "30–60 分钟": "30–60 min", "≥60 分钟": "≥60 min",
  "高": "high", "中": "medium", "低": "low",
};

/** Display name for a data label (category, lane, bin). Project names pass through. */
export function label(s: string): string {
  return current === "en" ? LABELS[s] ?? s : s;
}

// Attribution reasons are generated as "<kind> <detail>" in Chinese.
const REASONS: [RegExp, string][] = [
  [/^路径 (.*)$/, "path $1"], [/^规则 (.*)$/, "rule $1"], [/^关键词 (.*)$/, "keyword $1"],
  [/^语义判断[:：](.*)$/, "semantic: $1"], [/^按应用分类$/, "by app"], [/^未知应用$/, "unknown app"],
  [/^无工作目录$/, "no working directory"], [/^目录 (.*)$/, "directory $1"], [/^目录未登记$/, "directory not registered"],
  [/^人工修正$/, "manual correction"], [/^上下文[:：]此时你在给 (.*) 的 agent 发 prompt$/, "context: you were prompting the $1 agent"],
  [/^上下文[:：]前后都在 (.*)$/, "context: $1 before and after"],
];

/** English rendering of an attribution reason. */
export function reason(s: string): string {
  if (current !== "en" || !s) return s;
  for (const [re, en] of REASONS) if (re.test(s)) return s.replace(re, en);
  return s;
}
