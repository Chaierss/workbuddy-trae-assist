import { computed, ref } from "vue";
import { t } from "../lib/i18n";
import {
  api,
  type Account,
  type ClientDetection,
  type CredentialHealth,
  type CreditInfo,
  type Detection,
  type HealthInfo,
  type ScopeInfo,
  type Snapshot,
} from "../api";

/**
 * 轻量全局状态：单例 ref + 显式刷新函数。
 * 账号号池是**合并**的（一张表，平台在 account.platform 里），
 * 但「客户端检测」与「备份」仍是平台相关的（各自有进程与登录态目录）。
 * 因此保留一个「当前客户端」概念，只驱动检测/备份，不再切账号列表。
 */
export const scope = ref("workbuddy-cn");
export const scopes = ref<ScopeInfo[]>([]);
export const detection = ref<Detection | null>(null);
export const clientDetections = ref<ClientDetection[]>([]);
export const accounts = ref<Account[]>([]);
export const credits = ref<Record<string, CreditInfo>>({});
export const creditsLoading = ref(false);
export const snapshots = ref<Snapshot[]>([]);
export const health = ref<HealthInfo | null>(null);
export const loading = ref(false);

// ---------- 主题 ----------

/** 浅色 / 深色 / 跟随系统。只改样式变量，不参与任何业务逻辑。 */
export type ThemeMode = "light" | "dark" | "system";

const THEME_STORAGE_KEY = "chaierww.theme";

function readStoredTheme(): ThemeMode {
  const raw = localStorage.getItem(THEME_STORAGE_KEY);
  return raw === "light" || raw === "dark" ? raw : "system";
}

export const theme = ref<ThemeMode>(readStoredTheme());

const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");

/** system 时听系统的，其余模式直接给出定值。 */
export function resolvedTheme(mode: ThemeMode = theme.value): "light" | "dark" {
  if (mode !== "system") return mode;
  return prefersDark.matches ? "dark" : "light";
}

export function applyTheme(): void {
  document.documentElement.dataset.theme = resolvedTheme();
}

export function setTheme(next: ThemeMode): void {
  theme.value = next;
  localStorage.setItem(THEME_STORAGE_KEY, next);
  applyTheme();
}

// 系统主题变化只在「跟随系统」时生效；本文件在 main.ts 之前被导入，
// 所以这里落一次就能保证首屏颜色与 localStorage 一致。
prefersDark.addEventListener("change", () => {
  if (theme.value === "system") applyTheme();
});
applyTheme();

// ---------- 作用域写进 URL ----------

/** 与 router.ts 里 alias 一一对应：只有这些目的地带 `/s/<scope>` 前缀。 */
export const SCOPED_DESTINATIONS = [
  "/accounts",
  "/activity",
  "/usage",
  "/gateway",
];

/** 目的地的规范地址；不在名单里的路径原样返回（概览 / 设置不分作用域）。 */
export function scopedPath(path: string, key: string = scope.value): string {
  return SCOPED_DESTINATIONS.includes(path) ? `/s/${key}${path}` : path;
}

export function isImplementedScope(key: string): boolean {
  return scopes.value.some((item) => item.key === key && item.implemented);
}

export const currentScope = computed(
  () => scopes.value.find((item) => item.key === scope.value) ?? null,
);

export const accountCount = computed(() => accounts.value.length);

/** 侧栏活动角标：有可执行项的活动个数（不是账号数，避免和号池徽标重复）。 */
export const activityActionable = ref(0);

/** 侧栏健康角标：未确认（open）的事件数。打开健康中心或确认后应立即回落。 */
export const healthUnhandled = ref(0);

/**
 * 近 24 小时自动处置次数：和健康角标同一次请求返回，概览横幅直接复用，
 * 不额外打一次接口。读不到就是 null，横幅不渲染，不编 0。
 */
export const healthAutoStats = ref<{
  ok24h: number;
  failed24h: number;
  lastAt: string | null;
} | null>(null);

export async function loadHealthBadge(): Promise<void> {
  try {
    const data = await api.healthEvents("active", 1);
    healthUnhandled.value = data.summary.unhandled;
    healthAutoStats.value = data.summary.auto ?? null;
  } catch {
    // 角标取不到就不亮，不影响主界面。
    healthUnhandled.value = 0;
    healthAutoStats.value = null;
  }
}

export async function loadActivityActionable(): Promise<void> {
  try {
    const data = await api.overviewActionables();
    activityActionable.value = (data.campaigns ?? []).filter(
      (item) => item.actionable > 0,
    ).length;
  } catch {
    // 角标是辅助信息，取不到就不亮，不影响主界面。
    activityActionable.value = 0;
  }
}

const CREDENTIAL_WARN_MS = 7 * 24 * 60 * 60 * 1000;

function credentialExpiryMs(account: Account): number | null {
  if (!account.credentialExpiresAt) return null;
  const ms = new Date(account.credentialExpiresAt).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/**
 * 凭据问题号的唯一口径：状态不可用，或 7 天内到期 / 已过期。
 * 概览 KPI、号池健康卡与号池筛选都从这里取，避免三处数字各算各的。
 */
export const credentialIssueAccounts = computed(() => {
  const now = Date.now();
  return accounts.value.filter((item) => {
    if (item.credentialState !== "usable") return true;
    const expiresAt = credentialExpiryMs(item);
    return expiresAt !== null && expiresAt <= now + CREDENTIAL_WARN_MS;
  });
});

/** 互斥拆分：需重登 / 已过期 / 7 天内到期。 */
export const credentialIssueCounts = computed(() => {
  const now = Date.now();
  const counts = { relogin: 0, expired: 0, expiring: 0 };
  for (const item of credentialIssueAccounts.value) {
    if (item.credentialState === "relogin_required") {
      counts.relogin += 1;
      continue;
    }
    if (item.credentialState !== "usable") continue;
    const expiresAt = credentialExpiryMs(item);
    if (expiresAt === null) continue;
    if (expiresAt < now) counts.expired += 1;
    else counts.expiring += 1;
  }
  return counts;
});

export const attentionCount = computed(() => credentialIssueAccounts.value.length);

export const clientRunning = computed(() => detection.value?.running === true);

export const switchBlockedReason = computed(() => {
  if (!detection.value) return t("state.detecting");
  if (!detection.value.installed) return t("state.clientMissing");
  return null;
});

/**
 * 切号被挡时的下一步：只说原因用户也不知道怎么办。
 * - 检测中：给「重新检测」，重新拉一次客户端状态；
 * - 未安装：给「去设置 → 环境」，装客户端或改路径。
 */
export const switchBlockedAction = computed<"redetect" | "settings" | null>(() => {
  if (!detection.value) return "redetect";
  if (!detection.value.installed) return "settings";
  return null;
});

export async function loadScopes(): Promise<void> {
  const data = await api.scopes();
  scopes.value = data.scopes;
  const usable = data.scopes.filter((item) => item.implemented);
  if (usable.length && !usable.some((item) => item.key === scope.value)) {
    scope.value = usable[0].key;
  }
}

export async function loadHealth(): Promise<void> {
  health.value = await api.health();
}

export async function loadDetection(): Promise<void> {
  try {
    detection.value = await api.detect(scope.value);
  } catch {
    detection.value = null;
  }
}

/** 合并检测所有客户端，用于号池页顶部的安装检测状态条。 */
export async function loadDetections(): Promise<void> {
  try {
    const data = await api.detectAll();
    clientDetections.value = data.clients;
  } catch {
    clientDetections.value = [];
  }
}

export async function loadAccounts(): Promise<void> {
  // 合并号池：不分平台，一张表拿全。平台归属在 account.platform。
  const data = await api.mergedAccounts();
  accounts.value = data.accounts;
}

/**
 * 凭据完整性体检：返回全部账号，`gaps` 为空表示这个号凭据完整。
 * 和账号列表放在同一次 refreshAll 里刷新，避免「号池说缺凭据、健康中心没事件」。
 */
export const credentialHealth = ref<CredentialHealth | null>(null);

export async function loadCredentialHealth(): Promise<void> {
  try {
    credentialHealth.value = await api.credentialHealth();
  } catch {
    // 体检失败不影响号池，顶多不显示凭据提示。
    credentialHealth.value = null;
  }
}

export async function loadBackups(): Promise<void> {
  const data = await api.backups(scope.value);
  snapshots.value = data.snapshots;
}

/** 积分与到期时间。单独加载：它要发网络请求，失败不应拖垮号池列表。 */
export async function loadCredits(refresh = false): Promise<void> {
  if (!accounts.value.length) {
    credits.value = {};
    return;
  }
  creditsLoading.value = true;
  try {
    const data = await api.credits(refresh);
    credits.value = data.credits;
  } catch {
    // 查询失败时保留旧值（可能只是网络抖动），不清空以免界面闪成「未知」。
  } finally {
    creditsLoading.value = false;
  }
}

/** 全量刷新。顺序很重要：切换按钮的可用性依赖 detection，必须先拿到。 */
export async function refreshAll(): Promise<void> {
  loading.value = true;
  try {
    await loadDetection();
    await Promise.all([
      loadDetections(),
      loadAccounts(),
      loadCredentialHealth(),
      loadBackups(),
      loadActivityActionable(),
      loadHealthBadge(),
    ]);
    // 账号列表就绪后再查积分（需要账号 ID 集合）。
    await loadCredits();
  } finally {
    loading.value = false;
  }
}

export async function changeScope(next: string): Promise<void> {
  scope.value = next;
  snapshots.value = [];
  detection.value = null;
  // 账号列表是合并的，不随客户端切换清空；只刷新检测与备份。
  await refreshAll();
}
