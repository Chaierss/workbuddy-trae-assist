/**
 * 官方活动展示的共用工具。
 *
 * 三件事只写一份：
 * 1. 活动状态 → 文案 / 色调（卡片、抽屉、概览速览共用）；
 * 2. 活动主题 → 现有主题 token（不新增色板，暗色模式自动跟着变）；
 * 3. 官方外链白名单（后端目录已校验一次，前端再挡一次，避免渲染出非官方地址）。
 */

import type {
  ActivityCampaign,
  CampaignIcon,
  CampaignStatus,
  CampaignTheme,
} from "../api";
import type { MessageKey } from "./i18n";

/** 官方入口白名单：与后端 `OFFICIAL_URL_HOSTS` 保持一致。 */
const OFFICIAL_HOSTS = ["workbuddy.cn", "trae.cn"] as const;

/**
 * 把后端/检测结果里的地址收敛成可渲染的链接。
 *
 * - 只放行 https 且主机在白名单内；
 * - `workbuddy://` 是客户端深链，同样放行；
 * - 其余（http、javascript、data、外域）一律返回 null，调用方不要渲染 `<a>`。
 */
export function safeActivityUrl(raw: string | null | undefined): string | null {
  const value = (raw ?? "").trim();
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "workbuddy:") return value;
    if (url.protocol !== "https:") return null;
    const host = url.hostname.toLowerCase();
    const allowed = OFFICIAL_HOSTS.some(
      (domain) => host === domain || host.endsWith(`.${domain}`),
    );
    return allowed ? value : null;
  } catch {
    return null;
  }
}

interface StatusMeta {
  labelKey: MessageKey;
  tone: "neutral" | "brand" | "warn" | "danger" | "info";
}

/** 活动级状态文案。与账号级 state 分开，避免「未检测」被读成「没有活动」。 */
export const CAMPAIGN_STATUS: Record<CampaignStatus, StatusMeta> = {
  active: { labelKey: "activity.officialStatusActive", tone: "warn" },
  partial: { labelKey: "activity.officialStatusPartial", tone: "info" },
  done: { labelKey: "activity.officialStatusDone", tone: "brand" },
  unavailable: { labelKey: "activity.officialStatusUnavailable", tone: "neutral" },
  not_detected: { labelKey: "activity.officialStatusNotDetected", tone: "neutral" },
  no_accounts: { labelKey: "activity.officialStatusNoAccounts", tone: "neutral" },
  uncatalogued: { labelKey: "activity.officialStatusUncatalogued", tone: "warn" },
};

interface ThemeMeta {
  /** 图标底色 + 图标色。 */
  icon: string;
  /** 卡片顶部细色带。 */
  bar: string;
  /** 悬停描边。 */
  hover: string;
}

/**
 * 6 个主题映射到现有 4 组 token（brand / info / warn / neutral）。
 * 不引入新色板：暗色主题已经为这些 token 定义了覆写，新增十六进制颜色在暗色下会刺眼。
 */
export const CAMPAIGN_THEME: Record<CampaignTheme, ThemeMeta> = {
  green: {
    icon: "bg-brand-soft text-brand-dark",
    bar: "bg-brand",
    hover: "hover:border-brand/45",
  },
  blue: {
    icon: "bg-info-soft text-info",
    bar: "bg-info",
    hover: "hover:border-info/45",
  },
  amber: {
    icon: "bg-warn-soft text-warn",
    bar: "bg-warn",
    hover: "hover:border-warn/45",
  },
  violet: {
    icon: "bg-info-soft text-info",
    bar: "bg-info/70",
    hover: "hover:border-info/45",
  },
  teal: {
    icon: "bg-brand-soft text-brand-dark",
    bar: "bg-brand/70",
    hover: "hover:border-brand/45",
  },
  rose: {
    icon: "bg-warn-soft text-warn",
    bar: "bg-warn/70",
    hover: "hover:border-warn/45",
  },
};

/** 活动卡片的完成度：已完成账号 / 可检测账号，未检测不拉低进度。 */
export function campaignPercent(campaign: ActivityCampaign): number {
  if (!campaign.detectable) return 0;
  return Math.min(100, Math.round((campaign.done / campaign.detectable) * 100));
}

/** 活动期展示：只到日，避免长日期把卡片撑开。 */
export function campaignPeriod(
  start: string | null,
  end: string | null,
): string {
  const from = start?.slice(0, 10);
  const to = end?.slice(0, 10);
  if (!from && !to) return "";
  return `${from ?? "?"} ~ ${to ?? "?"}`;
}

/** 卡片按「主推 → 待办多 → 目录顺序」排序；已完成的沉到后面。 */
export function sortCampaigns(campaigns: ActivityCampaign[]): ActivityCampaign[] {
  return [...campaigns].sort((a, b) => {
    // 未收录活动永远沉底：它们没有规则/入口，不该抢占已维护活动的注意力。
    if ((a.status === "uncatalogued") !== (b.status === "uncatalogued")) {
      return a.status === "uncatalogued" ? 1 : -1;
    }
    if (a.allDone !== b.allDone) return a.allDone ? 1 : -1;
    if (a.featured !== b.featured) return a.featured ? -1 : 1;
    if (a.actionable !== b.actionable) return b.actionable - a.actionable;
    return a.sortOrder - b.sortOrder;
  });
}

/** 概览速览条只挑这几种：主推、有可执行项、能查到的。 */
export function pickHighlights(
  campaigns: ActivityCampaign[],
  limit = 3,
): ActivityCampaign[] {
  const candidates = campaigns.filter(
    (item) =>
      item.status !== "no_accounts" &&
      item.status !== "not_detected" &&
      item.status !== "uncatalogued",
  );
  return sortCampaigns(candidates).slice(0, limit);
}

/** 图标名 → 组件名，由调用方完成实际映射（避免 lib 依赖 lucide）。 */
export const CAMPAIGN_ICON_NAMES: Record<CampaignIcon, string> = {
  "calendar-check": "CalendarCheck",
  sprout: "Sprout",
  flame: "Flame",
  plane: "Plane",
  moon: "Moon",
  "graduation-cap": "GraduationCap",
  sun: "Sun",
};

/** 趋势图的一天（缺失的天补零，保证柱子等宽）。 */
export interface TrendPoint {
  day: string;
  label: string;
  ok: number;
  skipped: number;
  failed: number;
  total: number;
  reward: number;
}

/**
 * 把后端的稀疏趋势补成连续 `days` 天。
 *
 * 后端刻意只返回有记录的天（数据量恒定），补零必须在前端做 —— 但只有这里做，
 * 否则卡片/抽屉会各自算出不同的坐标。`until` 是后端给的「今天」，避免时区不同步。
 */
export function buildTrend(
  series: { days: { day: string; ok: number; skipped: number; failed: number; reward: number }[] } | undefined,
  days: number,
  until: string,
): TrendPoint[] {
  const byDay = new Map((series?.days ?? []).map((item) => [item.day, item]));
  const end = new Date(`${until}T00:00:00Z`);
  const points: TrendPoint[] = [];
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const moment = new Date(end.getTime() - offset * 86_400_000);
    const day = moment.toISOString().slice(0, 10);
    const value = byDay.get(day);
    const ok = value?.ok ?? 0;
    const skipped = value?.skipped ?? 0;
    const failed = value?.failed ?? 0;
    points.push({
      day,
      label: day.slice(5),
      ok,
      skipped,
      failed,
      total: ok + skipped + failed,
      reward: value?.reward ?? 0,
    });
  }
  return points;
}
