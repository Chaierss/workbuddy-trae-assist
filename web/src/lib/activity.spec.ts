import { describe, expect, it } from "vitest";
import {
  buildTrend,
  campaignPercent,
  pickHighlights,
  safeActivityUrl,
  sortCampaigns,
} from "./activity";
import type { ActivityCampaign } from "../api";

function campaign(patch: Partial<ActivityCampaign>): ActivityCampaign {
  return {
    key: "workbuddy.daily_checkin",
    platform: "workbuddy",
    group: "daily",
    title: "每日签到",
    subtitle: "",
    description: "",
    rewardHint: "",
    automatable: true,
    theme: "green",
    icon: "calendar-check",
    tags: [],
    rules: [],
    rewards: [],
    periodNote: "",
    officialUrl: "https://www.workbuddy.cn/",
    officialLabel: "WorkBuddy 官网",
    featured: false,
    sortOrder: 10,
    accountsTotal: 1,
    detectable: 1,
    done: 0,
    todo: 1,
    inProgress: 0,
    manual: 0,
    unavailable: 0,
    notDetected: 0,
    actionable: 1,
    allDone: false,
    status: "active",
    periodStart: null,
    periodEnd: null,
    ...patch,
  };
}

describe("safeActivityUrl", () => {
  it("只放行官方域名与 workbuddy 协议", () => {
    expect(safeActivityUrl("https://www.workbuddy.cn/")).toBe(
      "https://www.workbuddy.cn/",
    );
    expect(safeActivityUrl("https://trae.cn/activity/x")).toBe(
      "https://trae.cn/activity/x",
    );
    expect(safeActivityUrl("workbuddy://open/1")).toBe("workbuddy://open/1");
  });

  it("拒绝仿冒域名、http 与空值", () => {
    expect(safeActivityUrl("https://evil.com/")).toBeNull();
    expect(safeActivityUrl("https://workbuddy.cn.evil.com/")).toBeNull();
    expect(safeActivityUrl("http://www.workbuddy.cn/")).toBeNull();
    expect(safeActivityUrl("")).toBeNull();
    expect(safeActivityUrl(null)).toBeNull();
  });
});

describe("campaignPercent", () => {
  it("分母只用可检测账号，避免未检测把进度压低", () => {
    expect(campaignPercent(campaign({ detectable: 4, done: 2 }))).toBe(50);
    expect(campaignPercent(campaign({ detectable: 0, done: 0 }))).toBe(0);
    expect(campaignPercent(campaign({ detectable: 2, done: 5 }))).toBe(100);
  });
});

describe("sortCampaigns", () => {
  it("未收录活动沉底，未完成优先于已完成", () => {
    const sorted = sortCampaigns([
      campaign({ key: "c", status: "uncatalogued", sortOrder: 1 }),
      campaign({ key: "b", allDone: true, status: "done", sortOrder: 2 }),
      campaign({ key: "a", actionable: 2, sortOrder: 30 }),
    ]);
    expect(sorted.map((item) => item.key)).toEqual(["a", "b", "c"]);
  });

  it("主推活动在同等可执行数下优先", () => {
    const sorted = sortCampaigns([
      campaign({ key: "plain", sortOrder: 10 }),
      campaign({ key: "featured", featured: true, sortOrder: 20 }),
    ]);
    expect(sorted[0].key).toBe("featured");
  });
});

describe("pickHighlights", () => {
  it("过滤无账号、未检测与未收录", () => {
    const picked = pickHighlights(
      [
        campaign({ key: "a", status: "no_accounts" }),
        campaign({ key: "b", status: "not_detected" }),
        campaign({ key: "c", status: "uncatalogued" }),
        campaign({ key: "d" }),
      ],
      3,
    );
    expect(picked.map((item) => item.key)).toEqual(["d"]);
  });
});

describe("buildTrend", () => {
  it("把稀疏数据补成连续天数", () => {
    const points = buildTrend(
      {
        days: [
          { day: "2026-10-25", ok: 2, skipped: 1, failed: 0, reward: 5 },
        ],
      },
      3,
      "2026-10-26",
    );
    expect(points.map((item) => item.day)).toEqual([
      "2026-10-24",
      "2026-10-25",
      "2026-10-26",
    ]);
    expect(points[1]).toMatchObject({ ok: 2, skipped: 1, total: 3, reward: 5 });
    expect(points[0].total).toBe(0);
  });
});
