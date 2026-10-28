import type { ActivityRunReport, CredentialRefreshReport } from "../api";

export type RunStatus = "ok" | "skipped" | "failed";

export interface RunReportRow {
  /** 唯一行 key：账号 + 活动 key（凭据只有账号）。 */
  key: string;
  accountId: string | null;
  name: string;
  title: string;
  status: RunStatus;
  message: string;
  retryable: boolean;
}

export interface RunReportView {
  kind: "activity" | "credential";
  summary: { total: number; ok: number; skipped: number; failed: number };
  rows: RunReportRow[];
  /** 凭据报告的原模式：重试时沿用（force / check）。 */
  mode?: "due" | "force" | "check";
}

/** 活动执行报告 → 统一回执视图；状态与可重试标记原样保留。 */
export function normalizeActivityReport(report: ActivityRunReport): RunReportView {
  return {
    kind: "activity",
    summary: { ...report.summary },
    rows: report.results.map((item) => ({
      key: `${item.accountId}:${item.key}`,
      accountId: item.accountId,
      name: item.displayName,
      title: item.title,
      status: item.status,
      message: item.message,
      retryable: item.retryable,
    })),
  };
}

/** 凭据巡检结论 → 统一回执视图：续期成功算成功，无需处理算跳过。 */
export function normalizeCredentialReport(report: CredentialRefreshReport): RunReportView {
  const rows = report.results.map<RunReportRow>((item) => {
    const status: RunStatus =
      item.status === "refreshed" || item.status === "ok"
        ? "ok"
        : item.status === "noop" || item.status === "unsupported"
          ? "skipped"
          : "failed";
    return {
      key: item.accountId,
      accountId: item.accountId,
      name: item.displayName,
      title: item.status,
      status,
      message: item.detail,
      // 需重登不是「再试一次」能解决的，只对真正的失败给重试。
      retryable: item.status === "failed",
    };
  });
  return {
    kind: "credential",
    summary: {
      total: report.summary.total,
      ok: report.summary.refreshed + report.summary.ok,
      skipped: report.summary.noop + report.summary.unsupported,
      failed: report.summary.reloginRequired + report.summary.failed,
    },
    rows,
    mode: report.mode,
  };
}
