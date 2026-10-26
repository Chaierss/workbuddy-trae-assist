<script setup lang="ts">
/**
 * 用量页：请求与积分消耗的合并视图。
 *
 * 五个档位（同一份数据，换视角看）：
 *   总览   —— 四个统计卡 + 可切换指标/窗口的趋势图 + 模型占比环 + 升级后的账号卡；
 *   分布   —— 星期×小时热力图、单次消耗分布、账号×模型矩阵、最贵请求；
 *   对话   —— 按会话聚合的积分排行，点开看整条会话；
 *   对比   —— 一行一个账号的横表，可排序，不可查的沉底；
 *   明细   —— 全账号摊平成一张表：筛选/排序/分页，点行看这次请求聊了什么。
 *
 * 诚实边界（写死在界面，不能让用户误读）：
 *   1. 逐请求明细、按模型、按日趋势只有 WorkBuddy 有；Trae 只有积分消耗摘要。
 *   2. 两平台积分是**独立账本，不可互换**，合并数字只做展示侧合计，不能相加使用。
 *   3. 查不了就标「不可查」+ 原因，绝不显示成 0（0 会让人以为没用过）。
 *   4. 「对话」依赖本机 jsonl：云端只回计费行，requestId 与本机
 *      conversationRequestId 对得上才展示正文；对不上就如实说「本机无记录」。
 */
import { computed, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import {
  AlertTriangle,
  BarChart3,
  ChevronLeft,
  ChevronRight,
  Clock,
  Coins,
  Flame,
  Loader2,
  MessageSquare,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  X,
  Zap,
} from "lucide-vue-next";
import SectionCard from "../components/SectionCard.vue";
import PageHeader from "../components/PageHeader.vue";
import Skeleton from "../components/Skeleton.vue";
import SearchInput from "../components/SearchInput.vue";
import StatCard from "../components/StatCard.vue";
import StatusPill from "../components/StatusPill.vue";
import EmptyState from "../components/EmptyState.vue";
import SegmentedControl from "../components/SegmentedControl.vue";
import SelectMenu from "../components/SelectMenu.vue";
import {
  api,
  formatAmount,
  formatTime,
  type AccountUsage,
  type ConversationMessage,
  type ConversationUsage,
  type RequestRow,
  type UsageSummary,
  type UsageTurnBrief,
} from "../api";
import { localeTag, t, type MessageKey } from "../lib/i18n";
import { scopedPath } from "../stores/app";

const PLATFORM_LABEL: Record<string, string> = {
  workbuddy: "WorkBuddy",
  trae: "Trae",
};

type UsageTab = "overview" | "distribution" | "conversations" | "compare" | "detail";

const VIEWS: { key: UsageTab; labelKey: MessageKey }[] = [
  { key: "overview", labelKey: "usage.view.overview" },
  { key: "distribution", labelKey: "usage.view.distribution" },
  { key: "conversations", labelKey: "usage.view.conversations" },
  { key: "compare", labelKey: "usage.view.compare" },
  { key: "detail", labelKey: "usage.view.detail" },
];

const COMPARE_COLUMNS: { key: "today" | "week" | "month" | "total"; labelKey: MessageKey }[] = [
  { key: "today", labelKey: "usage.period.today" },
  { key: "week", labelKey: "usage.period.week" },
  { key: "month", labelKey: "usage.period.month" },
  { key: "total", labelKey: "usage.period.total" },
];
type CompareColumn = (typeof COMPARE_COLUMNS)[number]["key"];

/** 环图配色：都用主题变量混出来，深色模式自动跟着走。 */
const DONUT_COLORS = [
  "var(--color-brand)",
  "color-mix(in srgb, var(--color-brand) 70%, var(--color-info))",
  "var(--color-info)",
  "color-mix(in srgb, var(--color-info) 55%, var(--color-warn))",
  "var(--color-warn)",
  "var(--color-muted)",
];

const pool = ref<AccountUsage[]>([]);
const summary = ref<UsageSummary | null>(null);
const busy = ref(false);
const error = ref<string | null>(null);
const platformFilter = ref<"all" | "workbuddy" | "trae">("all");
const view = ref<UsageTab>("overview");

// 分段控件的选项（标签走 i18n，值保持字符串，回写时再收窄类型）。
const viewOptions = computed(() => VIEWS.map((item) => ({ value: item.key, label: t(item.labelKey) })));
const platformOptions = computed(() => [
  { value: "all", label: t("usage.platformAll") },
  { value: "workbuddy", label: "WorkBuddy" },
  { value: "trae", label: "Trae" },
]);
const metricOptions = computed(() => [
  { value: "credit", label: t("usage.trend.metricCredit") },
  { value: "requests", label: t("usage.trend.metricRequests") },
]);
const windowOptions = computed(() =>
  ([7, 14, 31] as const).map((days) => ({
    value: String(days),
    label: t("usage.trend.window", { days }),
  })),
);

// 趋势图
const windowDays = ref<7 | 14 | 31>(31);
const trendMetric = ref<"credit" | "requests">("credit");

// 对比
const sortKey = ref<CompareColumn>("total");
const sortDesc = ref(true);

// 明细
const detailAccount = ref("");
const detailModel = ref("");
const detailKeyword = ref("");
const detailPeriod = ref<"all" | "today" | "week" | "month">("all");
const detailSort = ref<"time" | "credit">("time");
const detailPage = ref(1);
const PAGE_SIZE = 50;

// 对话
const convQuery = ref("");

// 抽屉
interface DrawerState {
  kind: "request" | "conversation";
  title: string;
  subtitle: string;
  note: string | null;
  /** 单次请求的元信息（模型 / 客户端 / 积分 / 时间），会话抽屉为空。 */
  meta: { label: string; value: string }[];
  messages: ConversationMessage[];
  loading: boolean;
  error: string | null;
  notFound: boolean;
}
const drawer = ref<DrawerState | null>(null);
const MAX_DRAWER_MESSAGES = 200;

const visibleAccounts = computed(() =>
  pool.value.filter(
    (account) => platformFilter.value === "all" || account.platform === platformFilter.value,
  ),
);

const workbuddyAccounts = computed(() =>
  visibleAccounts.value.filter((account) => account.platform === "workbuddy" && account.detectable),
);

const traeAccounts = computed(() =>
  visibleAccounts.value.filter((account) => account.platform === "trae"),
);

function creditText(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return formatAmount(value);
}

function ratio(value: number, max: number): number {
  return Math.round((value / Math.max(1, max)) * 100);
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** 本地日期（CN 时区口径）→ `YYYY-MM-DD`，用于和服务端 `daily[].date` 对齐。 */
function isoOfOffset(days: number): string {
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() + days);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function shortDate(iso: string): string {
  return iso.slice(5);
}

function timeText(value: string): string {
  return value.length >= 16 ? value.slice(5, 16) : value;
}

function workspaceName(cwd: string): string {
  const name = (cwd || "").trim().replace(/[/\\]+$/, "").split(/[/\\]/).pop();
  return name || "—";
}

/** 毫秒时间戳 → ISO，交给 `formatTime` 做「刚刚 / 3 小时前」式展示。 */
function isoOf(ms: number | null): string | null {
  return typeof ms === "number" ? new Date(ms).toISOString() : null;
}

function parseRequestTime(value: string): Date | null {
  if (!value) return null;
  const date = new Date(value.replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? null : date;
}

// ---------- 总览：跨账号聚合 ----------

interface ModelAgg {
  model: string;
  requests: number;
  credit: number;
  accounts: number;
}

const modelAgg = computed<ModelAgg[]>(() => {
  const map = new Map<string, ModelAgg>();
  for (const account of workbuddyAccounts.value) {
    for (const model of account.models) {
      const current = map.get(model.model) ?? {
        model: model.model,
        requests: 0,
        credit: 0,
        accounts: 0,
      };
      current.requests += model.requests;
      current.credit += model.credit;
      current.accounts += 1;
      map.set(model.model, current);
    }
  }
  return [...map.values()].sort((a, b) => b.credit - a.credit);
});

interface DayAgg {
  date: string;
  requests: number;
  credit: number;
}

const dailyAgg = computed<DayAgg[]>(() => {
  const map = new Map<string, DayAgg>();
  for (const account of workbuddyAccounts.value) {
    for (const day of account.daily) {
      const current = map.get(day.date) ?? { date: day.date, requests: 0, credit: 0 };
      current.requests += day.requests;
      current.credit += day.credit;
      map.set(day.date, current);
    }
  }
  return [...map.values()].sort((a, b) => a.date.localeCompare(b.date));
});

/** 窗口内补齐日期（没有请求的日期按 0 画，这是「当天 0 次」而非「数据缺失」）。 */
const trendSeries = computed<DayAgg[]>(() => {
  const lookup = new Map(dailyAgg.value.map((day) => [day.date, day]));
  const points: DayAgg[] = [];
  for (let offset = -(windowDays.value - 1); offset <= 0; offset += 1) {
    const date = isoOfOffset(offset);
    const day = lookup.get(date);
    points.push({ date, requests: day?.requests ?? 0, credit: day?.credit ?? 0 });
  }
  return points;
});

function metricOf(day: DayAgg): number {
  return trendMetric.value === "credit" ? day.credit : day.requests;
}

const trendMax = computed(() => Math.max(0, ...trendSeries.value.map(metricOf)));

const trendGeometry = computed(() => {
  const values = trendSeries.value.map(metricOf);
  const max = Math.max(1, trendMax.value);
  const step = values.length > 1 ? 100 / (values.length - 1) : 0;
  const points = values.map((value, index) => ({
    x: index * step,
    y: 39 - (value / max) * 36,
  }));
  const line = points
    .map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(2)},${point.y.toFixed(2)}`)
    .join(" ");
  return { line, area: `${line} L100,39 L0,39 Z` };
});

const trendPeak = computed<DayAgg | null>(() => {
  const scored = trendSeries.value.filter((day) => metricOf(day) > 0);
  if (!scored.length) return null;
  return scored.reduce((best, day) => (metricOf(day) > metricOf(best) ? day : best));
});

const trendCompare = computed<{ text: string; tone: "up" | "down" | "flat" | "na" }>(() => {
  const days = windowDays.value;
  const currentStart = isoOfOffset(-(days - 1));
  const previousStart = isoOfOffset(-(2 * days - 1));
  const current = dailyAgg.value
    .filter((day) => day.date >= currentStart)
    .reduce((sum, day) => sum + metricOf(day), 0);
  const previous = dailyAgg.value
    .filter((day) => day.date >= previousStart && day.date < currentStart)
    .reduce((sum, day) => sum + metricOf(day), 0);
  const earliest = dailyAgg.value[0]?.date;
  // 前一个窗口没被数据覆盖时不硬比（否则「增长 999%」是假的）。
  if (!earliest || earliest > previousStart) {
    return { text: t("usage.trend.compareNa", { days }), tone: "na" };
  }
  const delta = current - previous;
  const unit = trendMetric.value === "credit" ? t("usage.unitCredit") : t("usage.unitRequests");
  const signed = `${delta > 0 ? "+" : ""}${creditText(delta)} ${unit}`;
  return {
    text: t("usage.trend.compare", { days, value: signed }),
    tone: delta > 0 ? "up" : delta < 0 ? "down" : "flat",
  };
});

interface ShareSlice {
  label: string;
  value: number;
  color: string;
  percent: number;
}

const shareSlices = computed<ShareSlice[]>(() => {
  const items = modelAgg.value;
  const total = items.reduce((sum, item) => sum + item.credit, 0);
  const top = items.slice(0, 5);
  const rest = items.slice(5).reduce((sum, item) => sum + item.credit, 0);
  const slices: ShareSlice[] = top.map((item, index) => ({
    label: item.model,
    value: item.credit,
    color: DONUT_COLORS[index % DONUT_COLORS.length],
    percent: total > 0 ? (item.credit / total) * 100 : 0,
  }));
  if (rest > 0) {
    slices.push({
      label: t("usage.share.other"),
      value: rest,
      color: DONUT_COLORS[5],
      percent: total > 0 ? (rest / total) * 100 : 0,
    });
  }
  return slices;
});

const donutSegments = computed(() => {
  const circumference = 2 * Math.PI * 42;
  let offset = 0;
  return shareSlices.value.map((slice) => {
    const length = (slice.percent / 100) * circumference;
    const segment = {
      ...slice,
      dash: `${length} ${circumference - length}`,
      offset: -offset,
    };
    offset += length;
    return segment;
  });
});

function dailyAverage(account: AccountUsage): number {
  return account.creditConsumed / 31;
}

function peakDay(account: AccountUsage): DayAgg | null {
  if (!account.daily.length) return null;
  return account.daily.reduce((best, day) => (day.credit > best.credit ? day : best));
}

// ---------- 分布 ----------

interface FlatRow {
  row: RequestRow;
  account: AccountUsage;
}

const allRows = computed<FlatRow[]>(() =>
  workbuddyAccounts.value.flatMap((account) =>
    account.detail.map((row) => ({ row, account })),
  ),
);

/** 明细表的对话摘要：按 `账号:请求` 缓存，翻页来回切不重复请求。 */
const turnBriefs = ref<Record<string, UsageTurnBrief>>({});
const briefLoading = ref(false);

function briefKey(accountId: string, requestId: string): string {
  return `${accountId}:${requestId}`;
}

function briefFor(accountId: string, requestId: string): UsageTurnBrief | null {
  return turnBriefs.value[briefKey(accountId, requestId)] ?? null;
}

async function loadBriefs(rows: FlatRow[]): Promise<void> {
  const groups = new Map<string, string[]>();
  for (const item of rows) {
    // 不按 conversationId 预筛：本机索引按 requestId 建，云端行没有会话 id 也可能有正文。
    if (turnBriefs.value[briefKey(item.account.accountId, item.row.requestId)]) continue;
    const ids = groups.get(item.account.accountId) ?? [];
    ids.push(item.row.requestId);
    groups.set(item.account.accountId, ids);
  }
  if (!groups.size) return;
  briefLoading.value = true;
  try {
    const results = await Promise.all(
      [...groups].map(([accountId, ids]) => api.usageTurns(accountId, ids)),
    );
    const merged = { ...turnBriefs.value };
    for (const data of results) {
      for (const [requestId, brief] of Object.entries(data.turns)) {
        merged[briefKey(data.accountId, requestId)] = brief;
      }
    }
    turnBriefs.value = merged;
  } catch {
    // 摘要失败不阻断明细表；行内会显示「本机无记录」。
  } finally {
    briefLoading.value = false;
  }
}

const weekdayLabels = computed(() => {
  const formatter = new Intl.DateTimeFormat(localeTag(), { weekday: "short" });
  return Array.from({ length: 7 }, (_, index) => formatter.format(new Date(2024, 0, 1 + index)));
});

const heatGrid = computed(() => {
  const grid = Array.from({ length: 7 }, () =>
    Array.from({ length: 24 }, () => ({ requests: 0, credit: 0 })),
  );
  for (const item of allRows.value) {
    const date = parseRequestTime(item.row.requestTime);
    if (!date) continue;
    const day = (date.getDay() + 6) % 7; // 周一开始
    const cell = grid[day][date.getHours()];
    cell.requests += 1;
    cell.credit += item.row.credit;
  }
  return grid;
});

const heatMax = computed(() =>
  Math.max(0, ...heatGrid.value.flatMap((row) => row.map((cell) => cell.requests))),
);

function heatStyle(day: number, hour: number): Record<string, string> {
  const cell = heatGrid.value[day][hour];
  if (!cell.requests) return { background: "var(--color-line-soft)" };
  const intensity = 18 + Math.round((cell.requests / Math.max(1, heatMax.value)) * 72);
  return {
    background: `color-mix(in srgb, var(--color-brand) ${intensity}%, var(--color-surface))`,
  };
}

function heatTitle(day: number, hour: number): string {
  const cell = heatGrid.value[day][hour];
  return `${weekdayLabels.value[day]} ${pad(hour)}:00 · ${cell.requests} ${t("usage.unitRequests")} · ${creditText(cell.credit)} ${t("usage.unitCredit")}`;
}

const credits = computed(() =>
  allRows.value.map((item) => item.row.credit).sort((a, b) => a - b),
);

function quantile(sorted: number[], q: number): number {
  if (!sorted.length) return 0;
  const position = (sorted.length - 1) * q;
  const base = Math.floor(position);
  const rest = position - base;
  const next = sorted[base + 1];
  return next === undefined ? sorted[base] : sorted[base] + rest * (next - sorted[base]);
}

const creditStats = computed(() => {
  const values = credits.value;
  const total = values.reduce((sum, value) => sum + value, 0);
  return {
    count: values.length,
    mean: values.length ? total / values.length : 0,
    median: quantile(values, 0.5),
    p90: quantile(values, 0.9),
    max: values.length ? values[values.length - 1] : 0,
    total,
  };
});

/** 总览补充维度：平均单次消耗 + 对话可还原率。 */
const avgCost = computed(() =>
  creditStats.value.count ? creditStats.value.total / creditStats.value.count : 0,
);

const linkRate = computed(() => {
  const { linked, total } = conversationLinked.value;
  return total ? Math.round((linked / total) * 100) : null;
});

interface ClientAgg {
  client: string;
  requests: number;
  credit: number;
}

/** 客户端维度：同一批请求按来源再切一刀（IDE / CLI / 插件各花了多少）。 */
const clientAgg = computed<ClientAgg[]>(() => {
  const map = new Map<string, ClientAgg>();
  for (const item of allRows.value) {
    const client = item.row.client || "—";
    const current = map.get(client) ?? { client, requests: 0, credit: 0 };
    current.requests += 1;
    current.credit += item.row.credit;
    map.set(client, current);
  }
  return [...map.values()].sort((a, b) => b.credit - a.credit);
});

const clientMax = computed(() => Math.max(1, ...clientAgg.value.map((item) => item.credit)));

interface ModelEfficiency {
  model: string;
  requests: number;
  credit: number;
  average: number;
}

/** 模型效率：均耗越高越贵，用来判断「换模型能省多少」。 */
const modelEfficiency = computed<ModelEfficiency[]>(() =>
  modelAgg.value
    .filter((item) => item.requests > 0)
    .map((item) => ({ ...item, average: item.credit / item.requests }))
    .sort((a, b) => b.average - a.average)
    .slice(0, 8),
);

const efficiencyMax = computed(() =>
  Math.max(0.0001, ...modelEfficiency.value.map((item) => item.average)),
);

const CREDIT_BUCKETS = [
  { from: 0, to: 1 },
  { from: 1, to: 5 },
  { from: 5, to: 20 },
  { from: 20, to: 50 },
  { from: 50, to: Number.POSITIVE_INFINITY },
];

const bucketStats = computed(() => {
  const counts = CREDIT_BUCKETS.map(() => 0);
  for (const value of credits.value) {
    const index = CREDIT_BUCKETS.findIndex(
      (bucket) => value >= bucket.from && value < bucket.to,
    );
    if (index >= 0) counts[index] += 1;
  }
  const max = Math.max(0, ...counts);
  return CREDIT_BUCKETS.map((bucket, index) => ({
    label:
      bucket.to === Number.POSITIVE_INFINITY
        ? t("usage.dist.bucketOver", { from: bucket.from })
        : t("usage.dist.bucket", { from: bucket.from, to: bucket.to }),
    count: counts[index],
    percent: max > 0 ? Math.max(3, Math.round((counts[index] / max) * 100)) : 0,
  }));
});

const topRequests = computed(() =>
  [...allRows.value].sort((a, b) => b.row.credit - a.row.credit).slice(0, 10),
);

const matrixModels = computed(() => modelAgg.value.slice(0, 8).map((item) => item.model));

const matrixMax = computed(() => {
  let max = 0;
  for (const account of workbuddyAccounts.value) {
    for (const model of account.models) {
      if (matrixModels.value.includes(model.model)) max = Math.max(max, model.credit);
    }
  }
  return max;
});

function matrixValue(account: AccountUsage, model: string): number | null {
  const found = account.models.find((item) => item.model === model);
  return found ? found.credit : null;
}

function matrixStyle(value: number | null): Record<string, string> {
  if (value === null) return {};
  const intensity = 14 + Math.round((value / Math.max(1, matrixMax.value)) * 66);
  return {
    background: `color-mix(in srgb, var(--color-brand) ${intensity}%, var(--color-surface))`,
  };
}

// ---------- 对话 ----------

interface ConversationRow {
  account: AccountUsage;
  conversation: ConversationUsage;
}

const conversationRows = computed<ConversationRow[]>(() => {
  const keyword = convQuery.value.trim().toLowerCase();
  return workbuddyAccounts.value
    .flatMap((account) =>
      account.conversations.map((conversation) => ({ account, conversation })),
    )
    .filter((item) => {
      if (!keyword) return true;
      return (
        item.conversation.title.toLowerCase().includes(keyword) ||
        item.conversation.cwd.toLowerCase().includes(keyword) ||
        item.account.displayName.toLowerCase().includes(keyword)
      );
    })
    .sort((a, b) => b.conversation.credit - a.conversation.credit);
});

const conversationLinked = computed(() => {
  const total = workbuddyAccounts.value.reduce(
    (sum, account) => sum + account.detail.length,
    0,
  );
  const linked = workbuddyAccounts.value.reduce(
    (sum, account) => sum + account.linkedRequests,
    0,
  );
  return { total, linked };
});

// ---------- 对比 ----------

function columnValue(
  account: AccountUsage,
  key: CompareColumn,
): { requests: number | null; credit: number | null } {
  switch (key) {
    case "today":
      return { requests: account.todayRequests, credit: account.todayCredit };
    case "week":
      return { requests: account.weekRequests, credit: account.weekCredit };
    case "month":
      return { requests: account.monthRequests, credit: account.monthCredit };
    default:
      // Trae 没有周期性请求量，只有累计积分 —— 请求列显示「—」，不假装是 0。
      return account.platform === "trae"
        ? { requests: null, credit: account.creditsConsumed }
        : { requests: account.requests, credit: account.creditConsumed };
  }
}

const compareRows = computed(() => {
  const rows = visibleAccounts.value.map((account) => ({
    account,
    cells: Object.fromEntries(
      COMPARE_COLUMNS.map((column) => [column.key, columnValue(account, column.key)]),
    ) as Record<CompareColumn, { requests: number | null; credit: number | null }>,
  }));
  const detectable = rows.filter((row) => row.account.detectable);
  const blind = rows.filter((row) => !row.account.detectable);
  detectable.sort((a, b) => {
    const left = a.cells[sortKey.value];
    const right = b.cells[sortKey.value];
    const leftValue = left.requests ?? left.credit ?? 0;
    const rightValue = right.requests ?? right.credit ?? 0;
    const diff = leftValue - rightValue;
    if (diff !== 0) return sortDesc.value ? -diff : diff;
    return (a.account.displayName ?? "").localeCompare(b.account.displayName ?? "");
  });
  return [...detectable, ...blind];
});

function toggleSort(key: CompareColumn): void {
  if (sortKey.value === key) sortDesc.value = !sortDesc.value;
  else {
    sortKey.value = key;
    sortDesc.value = true;
  }
}

// ---------- 明细：筛选 / 排序 / 分页 ----------

const modelOptions = computed(() => {
  const models = new Set<string>();
  for (const item of allRows.value) models.add(item.row.model);
  return [...models].sort((a, b) => a.localeCompare(b));
});

// 明细筛选下拉：空值项即「全部」，这样选中态和占位文案共用一套标签。
const detailAccountOptions = computed(() => [
  { value: "", label: t("usage.detail.filterAccount") },
  ...workbuddyAccounts.value.map((account) => ({
    value: account.accountId,
    label: account.displayName,
  })),
]);
const detailModelOptions = computed(() => [
  { value: "", label: t("usage.detail.filterModel") },
  ...modelOptions.value.map((model) => ({ value: model, label: model })),
]);
const periodOptions = computed<Array<{ value: "all" | "today" | "week" | "month"; label: string }>>(
  () => [
    { value: "all", label: t("usage.detail.periodAll") },
    { value: "today", label: t("usage.period.today") },
    { value: "week", label: t("usage.period.week") },
    { value: "month", label: t("usage.period.total") },
  ],
);
const detailSortOptions = computed<Array<{ value: "time" | "credit"; label: string }>>(() => [
  { value: "time", label: t("usage.detail.sortTime") },
  { value: "credit", label: t("usage.detail.sortCredit") },
]);

function periodStart(period: "all" | "today" | "week" | "month"): string | null {
  if (period === "all") return null;
  if (period === "today") return isoOfOffset(0);
  if (period === "week") return isoOfOffset(-6);
  return isoOfOffset(-30);
}

const detailRows = computed<FlatRow[]>(() => {
  const keyword = detailKeyword.value.trim().toLowerCase();
  const start = periodStart(detailPeriod.value);
  const rows = allRows.value.filter((item) => {
    if (detailAccount.value && item.account.accountId !== detailAccount.value) return false;
    if (detailModel.value && item.row.model !== detailModel.value) return false;
    if (start && item.row.date < start) return false;
    if (keyword) {
      const brief = turnBriefs.value[briefKey(item.account.accountId, item.row.requestId)];
      const haystack = `${item.row.model} ${item.row.client} ${item.account.displayName} ${
        brief?.userText ?? ""
      } ${brief?.assistantText ?? ""}`;
      if (!haystack.toLowerCase().includes(keyword)) return false;
    }
    return true;
  });
  rows.sort((a, b) => {
    if (detailSort.value === "credit") return b.row.credit - a.row.credit;
    return b.row.requestTime.localeCompare(a.row.requestTime);
  });
  return rows;
});

const detailPages = computed(() => Math.max(1, Math.ceil(detailRows.value.length / PAGE_SIZE)));

const pagedRows = computed(() =>
  detailRows.value.slice((detailPage.value - 1) * PAGE_SIZE, detailPage.value * PAGE_SIZE),
);

watch([detailAccount, detailModel, detailKeyword, detailPeriod, detailSort], () => {
  detailPage.value = 1;
});

watch(detailPages, (pages) => {
  if (detailPage.value > pages) detailPage.value = pages;
});

/** 明细表 / 分布页的对话摘要：按需懒加载，来回切换不重复请求。 */
watch(
  [view, pagedRows],
  ([current, rows]) => {
    if (current === "detail") void loadBriefs(rows);
    // 分布页的「最贵单次请求」也直接展示聊了什么，同样只取这 10 条。
    if (current === "distribution") void loadBriefs(topRequests.value);
  },
  { immediate: true },
);

// ---------- 抽屉 ----------

const shownDrawerMessages = computed(() =>
  drawer.value ? drawer.value.messages.slice(-MAX_DRAWER_MESSAGES) : [],
);

function closeDrawer(): void {
  drawer.value = null;
}

/** 摘要 → 对话气泡：本机只索引到请求/回答摘要时，也把这两条展示出来。 */
function briefMessages(brief: UsageTurnBrief | null): ConversationMessage[] {
  if (!brief) return [];
  const messages: ConversationMessage[] = [];
  if (brief.userText) {
    messages.push({ role: "user", text: brief.userText, at: null, model: null });
  }
  if (brief.assistantText) {
    messages.push({ role: "assistant", text: brief.assistantText, at: null, model: brief.model });
  }
  return messages;
}

/** 按需补一条摘要（分布页直接点开时，明细页可能还没批量拉过）。 */
async function ensureBrief(account: AccountUsage, row: RequestRow): Promise<UsageTurnBrief | null> {
  const cached = briefFor(account.accountId, row.requestId);
  if (cached) return cached;
  try {
    const data = await api.usageTurns(account.accountId, [row.requestId]);
    const brief = data.turns[row.requestId] ?? null;
    if (brief) {
      turnBriefs.value = {
        ...turnBriefs.value,
        [briefKey(account.accountId, row.requestId)]: brief,
      };
    }
    return brief;
  } catch {
    return null;
  }
}

async function openRequestTurn(account: AccountUsage, row: RequestRow): Promise<void> {
  const subtitle = `${account.displayName} · ${row.requestTime} · ${creditText(row.credit)} ${t("usage.unitCredit")}`;
  drawer.value = {
    kind: "request",
    title: t("usage.drawer.requestTitle"),
    subtitle,
    note: null,
    meta: [
      { label: t("usage.detail.model"), value: row.model },
      { label: t("usage.detail.client"), value: row.client || "—" },
      { label: t("usage.detail.credit"), value: `${creditText(row.credit)} ${t("usage.unitCredit")}` },
      { label: t("usage.detail.time"), value: row.requestTime },
    ],
    messages: [],
    loading: true,
    error: null,
    notFound: false,
  };
  const current = drawer.value;

  // 云端明细里不少行没有 conversationId（在别的设备产生 / 本机记录已清理）。
  // 这种行不静默不响应：用本机索引到的摘要先把「问了什么、答了什么」展示出来。
  if (!row.conversationId) {
    const brief = await ensureBrief(account, row);
    if (drawer.value !== current) return;
    const messages = briefMessages(brief);
    drawer.value.messages = messages;
    drawer.value.loading = false;
    if (messages.length) {
      drawer.value.note = t("usage.drawer.briefNote");
    } else {
      drawer.value.notFound = true;
      drawer.value.error = brief?.error ?? null;
    }
    return;
  }

  try {
    const data = await api.usageRequestTurn(account.accountId, row.requestId);
    if (drawer.value !== current) return;
    if (data.found && data.messages.length) {
      drawer.value.title = data.title || t("usage.conv.unknown");
      drawer.value.subtitle = `${account.displayName} · ${row.requestTime}`;
      drawer.value.messages = data.messages;
      return;
    }
    // 没有完整正文但有摘要：仍给出能看懂的内容，并说明完整对话不在本机。
    const messages = briefMessages(briefFor(account.accountId, row.requestId));
    if (messages.length) {
      drawer.value.messages = messages;
      drawer.value.note = t("usage.drawer.briefNote");
      return;
    }
    drawer.value.notFound = true;
    drawer.value.error = data.error;
  } catch (cause) {
    if (drawer.value !== current) return;
    drawer.value.error = (cause as Error).message;
  } finally {
    if (drawer.value === current) drawer.value.loading = false;
  }
}

async function openConversation(account: AccountUsage, conversation: ConversationUsage): Promise<void> {
  drawer.value = {
    kind: "conversation",
    title: conversation.title || t("usage.conv.unknown"),
    subtitle: `${account.displayName} · ${workspaceName(conversation.cwd)} · ${t("usage.conv.requests", { count: conversation.requests })}`,
    note: t("usage.conv.dialogNote"),
    meta: [],
    messages: [],
    loading: true,
    error: null,
    notFound: false,
  };
  const current = drawer.value;
  try {
    const data = await api.conversationMessages(account.accountId, conversation.conversationId);
    if (drawer.value !== current) return;
    drawer.value.messages = data.messages;
  } catch (cause) {
    if (drawer.value !== current) return;
    drawer.value.error = (cause as Error).message;
  } finally {
    if (drawer.value === current) drawer.value.loading = false;
  }
}

// ---------- 加载 ----------

async function load(refresh = false): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const data = await api.usage(refresh);
    pool.value = data.accounts;
    summary.value = data.summary;
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div>
    <PageHeader :title="t('usage.title')">
      <template #subtitle>
        {{ t("usage.subtitleLead") }}<span class="text-ink-soft">{{ t("usage.subtitleKey") }}</span>{{ t("usage.subtitleTail") }}
      </template>
      <template #actions>
        <SegmentedControl
          :model-value="view"
          :options="viewOptions"
          :label="t('usage.title')"
          @update:model-value="view = $event as UsageTab"
        />
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-2 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-60"
          :disabled="busy"
          @click="load(true)"
        >
          <RefreshCw :size="14" :stroke-width="1.9" :class="busy ? 'animate-spin' : ''" />
          {{ t("common.refresh") }}
        </button>
      </template>
    </PageHeader>

    <div v-if="view === 'overview'" class="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      <StatCard
        :label="t('usage.stat.accounts')"
        :value="`${summary?.detectable ?? 0}/${summary?.accounts ?? 0}`"
        :icon="BarChart3"
        :hint="t('usage.stat.accountsHint')"
      />
      <StatCard
        :label="t('usage.stat.requests')"
        :value="String(summary?.totalRequests ?? 0)"
        :icon="Zap"
        :hint="t('usage.stat.requestsHint')"
      />
      <StatCard
        :label="t('usage.stat.workbuddy')"
        :value="creditText(summary?.workbuddyCreditConsumed)"
        :icon="Coins"
        :hint="t('usage.stat.workbuddyHint')"
      />
      <StatCard
        :label="t('usage.stat.trae')"
        :value="summary?.traeCreditsConsumed != null ? creditText(summary.traeCreditsConsumed) : '—'"
        :icon="Coins"
        :hint="t('usage.stat.traeHint')"
      />
      <StatCard
        :label="t('usage.stat.avgCost')"
        :value="creditText(avgCost)"
        :icon="TrendingUp"
        :hint="t('usage.stat.avgCostHint')"
      />
      <StatCard
        :label="t('usage.stat.linkRate')"
        :value="linkRate == null ? '—' : `${linkRate}%`"
        :icon="MessageSquare"
        :hint="t('usage.stat.linkRateHint')"
      />
    </div>

    <div
      v-if="error"
      class="mt-4 rounded-card border border-line bg-danger-soft px-5 py-3 text-body-sm text-danger"
    >
      {{ t("usage.error", { message: error }) }}
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-3">
      <SegmentedControl
        :model-value="platformFilter"
        :options="platformOptions"
        :label="t('usage.platformAll')"
        size="sm"
        @update:model-value="platformFilter = $event as 'all' | 'workbuddy' | 'trae'"
      />
      <span class="text-body-sm text-muted">{{ t("usage.accountCount", { count: visibleAccounts.length }) }}</span>
    </div>

    <div v-if="busy && !pool.length" class="mt-4">
      <div class="mb-3 flex items-center gap-2 text-body-sm text-muted">
        <Loader2 :size="14" class="animate-spin" /> {{ t("usage.loading") }}
      </div>
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Skeleton v-for="index in 3" :key="index" variant="card" />
      </div>
    </div>

    <div v-else-if="!visibleAccounts.length" class="mt-4">
      <SectionCard>
        <EmptyState :title="t('usage.noAccounts')" :hint="t('usage.noAccountsHint')">
          <template #action>
            <RouterLink
              :to="scopedPath('/accounts')"
              class="inline-flex items-center gap-1.5 rounded-control bg-brand px-3 py-1.5 text-body-sm font-medium text-white transition-colors hover:bg-brand-dark"
            >
              {{ t("usage.goAccounts") }}
            </RouterLink>
          </template>
        </EmptyState>
      </SectionCard>
    </div>

    <!-- ================= 总览 ================= -->
    <div v-else-if="view === 'overview'" class="mt-4 space-y-4">
      <SectionCard :title="t('usage.trend.title')" :subtitle="t('usage.trend.subtitle')">
        <template #actions>
          <div class="flex items-center gap-2">
            <SegmentedControl
              :model-value="trendMetric"
              :options="metricOptions"
              size="sm"
              :label="t('usage.trend.title')"
              @update:model-value="trendMetric = $event as 'credit' | 'requests'"
            />
            <SegmentedControl
              :model-value="String(windowDays)"
              :options="windowOptions"
              size="sm"
              :label="t('usage.trend.window', { days: windowDays })"
              @update:model-value="windowDays = Number($event) as 7 | 14 | 31"
            />
          </div>
        </template>

        <div v-if="trendSeries.length">
          <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-caption">
            <span
              class="inline-flex items-center gap-1"
              :class="
                trendCompare.tone === 'up'
                  ? 'text-warn'
                  : trendCompare.tone === 'down'
                    ? 'text-brand'
                    : 'text-muted'
              "
            >
              <TrendingUp v-if="trendCompare.tone === 'up'" :size="13" />
              <TrendingDown v-else-if="trendCompare.tone === 'down'" :size="13" />
              <Clock v-else :size="13" />
              {{ trendCompare.text }}
            </span>
            <span v-if="trendPeak" class="text-muted">
              {{
                t("usage.trend.peak", {
                  date: shortDate(trendPeak.date),
                  value: `${creditText(metricOf(trendPeak))} ${
                    trendMetric === 'credit' ? t('usage.unitCredit') : t('usage.unitRequests')
                  }`,
                })
              }}
            </span>
          </div>

          <div class="relative mt-3 h-[170px]">
            <svg
              class="absolute inset-0 h-full w-full"
              viewBox="0 0 100 40"
              preserveAspectRatio="none"
            >
              <defs>
                <linearGradient id="usage-trend-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="var(--color-brand)" stop-opacity="0.26" />
                  <stop offset="100%" stop-color="var(--color-brand)" stop-opacity="0.02" />
                </linearGradient>
              </defs>
              <path :d="trendGeometry.area" fill="url(#usage-trend-fill)" />
              <path
                :d="trendGeometry.line"
                fill="none"
                stroke="var(--color-brand)"
                stroke-width="1.4"
                vector-effect="non-scaling-stroke"
                stroke-linejoin="round"
              />
            </svg>
            <div class="absolute inset-0 flex">
              <div
                v-for="day in trendSeries"
                :key="day.date"
                class="h-full flex-1"
                :title="
                  t('usage.dayTooltip', {
                    date: day.date,
                    credit: creditText(day.credit),
                    requests: day.requests,
                  })
                "
              />
            </div>
          </div>
          <div class="mt-1 flex justify-between text-micro text-muted">
            <span>{{ shortDate(trendSeries[0]?.date ?? "") }}</span>
            <span>{{ shortDate(trendSeries[Math.floor(trendSeries.length / 2)]?.date ?? "") }}</span>
            <span>{{ shortDate(trendSeries[trendSeries.length - 1]?.date ?? "") }}</span>
          </div>
          <div v-if="traeAccounts.length" class="mt-2 text-caption text-muted">
            {{ t("usage.trend.traeNote") }}
          </div>
        </div>
        <EmptyState v-else :title="t('usage.trend.empty')" />
      </SectionCard>

      <div class="grid gap-4 lg:grid-cols-2">
        <SectionCard :title="t('usage.share.title')" :subtitle="t('usage.share.subtitle')">
          <div v-if="shareSlices.length" class="flex items-center gap-5">
            <div class="relative shrink-0">
              <svg viewBox="0 0 100 100" class="h-[136px] w-[136px] -rotate-90">
                <circle
                  cx="50"
                  cy="50"
                  r="42"
                  fill="none"
                  stroke="var(--color-line-soft)"
                  stroke-width="12"
                />
                <circle
                  v-for="segment in donutSegments"
                  :key="segment.label"
                  cx="50"
                  cy="50"
                  r="42"
                  fill="none"
                  :stroke="segment.color"
                  stroke-width="12"
                  :stroke-dasharray="segment.dash"
                  :stroke-dashoffset="segment.offset"
                />
              </svg>
              <div class="absolute inset-0 flex flex-col items-center justify-center">
                <span class="text-caption text-muted">{{ t("usage.detail.credit") }}</span>
                <span class="text-title font-semibold text-ink">{{ creditText(creditStats.total) }}</span>
              </div>
            </div>
            <ul class="min-w-0 flex-1 space-y-1.5">
              <li
                v-for="slice in shareSlices"
                :key="slice.label"
                class="flex items-center gap-2 text-body-sm"
              >
                <span class="h-2 w-2 shrink-0 rounded-full" :style="{ background: slice.color }" />
                <span class="min-w-0 flex-1 truncate text-ink-soft" :title="slice.label">
                  {{ slice.label }}
                </span>
                <span class="shrink-0 text-muted">{{ creditText(slice.value) }}</span>
                <span class="w-10 shrink-0 text-right text-caption text-muted">
                  {{ slice.percent.toFixed(0) }}%
                </span>
              </li>
            </ul>
          </div>
          <EmptyState v-else :title="t('usage.share.empty')" />
        </SectionCard>

        <SectionCard :title="t('usage.modelAgg.title')" :subtitle="t('usage.modelAgg.subtitle')">
          <div v-if="modelAgg.length" class="space-y-2">
            <div
              v-for="item in modelAgg.slice(0, 8)"
              :key="item.model"
              class="flex items-center gap-3 text-body-sm"
            >
              <span class="w-40 shrink-0 truncate text-ink-soft" :title="item.model">
                {{ item.model }}
              </span>
              <div class="h-2 flex-1 overflow-hidden rounded-full bg-line-soft">
                <div
                  class="h-full rounded-full bg-brand"
                  :style="{ width: `${ratio(item.credit, modelAgg[0]?.credit ?? 1)}%` }"
                />
              </div>
              <span class="w-20 shrink-0 text-right text-muted">
                {{ creditText(item.credit) }} {{ t("usage.unitCredit") }}
              </span>
              <span class="w-14 shrink-0 text-right text-muted">
                {{ item.requests }} {{ t("usage.unitRequests") }}
              </span>
              <span class="w-14 shrink-0 text-right text-caption text-muted">
                {{ item.accounts }} {{ t("usage.unitAccounts") }}
              </span>
            </div>
          </div>
          <EmptyState v-else :title="t('usage.modelAgg.empty')" />
        </SectionCard>
      </div>

      <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <article
          v-for="account in visibleAccounts"
          :key="account.accountId"
          class="flex flex-col gap-3 rounded-card border border-line bg-surface p-4 shadow-card transition-colors hover:border-line-strong"
        >
          <header class="flex items-center gap-2">
            <span class="min-w-0 flex-1 truncate text-body font-medium text-ink">
              {{ account.displayName }}
            </span>
            <span class="shrink-0 text-caption text-muted">
              {{ PLATFORM_LABEL[account.platform] ?? account.platform }}
            </span>
            <StatusPill v-if="!account.detectable" tone="danger">{{ t("usage.undetectable") }}</StatusPill>
          </header>

          <div
            v-if="!account.detectable"
            class="flex items-start gap-2 text-caption leading-relaxed text-muted"
          >
            <AlertTriangle :size="14" class="mt-0.5 shrink-0 text-warn" />
            <span>{{ account.error ?? t("usage.unsupportedCredential") }}</span>
          </div>

          <!-- Trae：累计积分 + 剩余额度进度条 -->
          <template v-else-if="account.platform === 'trae'">
            <div class="flex items-baseline gap-6">
              <div>
                <div class="text-caption text-muted">{{ t("usage.trae.consumed") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink">
                  {{ creditText(account.creditsConsumed) }}
                </div>
              </div>
              <div>
                <div class="text-caption text-muted">{{ t("usage.trae.total") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink-soft">
                  {{ creditText(account.creditsTotal) }}
                </div>
              </div>
            </div>
            <div v-if="account.creditsTotal" class="space-y-1">
              <div class="flex items-center justify-between text-caption text-muted">
                <span>{{ t("usage.trae.remaining") }}</span>
                <span>
                  {{ creditText(Math.max(0, account.creditsTotal - (account.creditsConsumed ?? 0))) }}
                  / {{ creditText(account.creditsTotal) }}
                </span>
              </div>
              <div class="h-2 overflow-hidden rounded-full bg-line-soft">
                <div
                  class="h-full rounded-full bg-brand"
                  :style="{
                    width: `${ratio(
                      Math.max(0, account.creditsTotal - (account.creditsConsumed ?? 0)),
                      account.creditsTotal,
                    )}%`,
                  }"
                />
              </div>
              <div class="text-micro text-muted">{{ t("usage.trae.remainingHint") }}</div>
            </div>
            <div class="text-caption text-muted">{{ t("usage.trae.note") }}</div>
          </template>

          <!-- WorkBuddy：周期 + 日均 / 峰值 / 预测 + 迷你柱状 -->
          <template v-else>
            <div class="grid grid-cols-3 gap-2">
              <div>
                <div class="text-caption text-muted">{{ t("usage.period.today") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink">{{ account.todayRequests }}</div>
                <div class="text-caption text-ink-soft">
                  {{ creditText(account.todayCredit) }} {{ t("usage.unitCredit") }}
                </div>
              </div>
              <div>
                <div class="text-caption text-muted">{{ t("usage.period.week") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink">{{ account.weekRequests }}</div>
                <div class="text-caption text-ink-soft">
                  {{ creditText(account.weekCredit) }} {{ t("usage.unitCredit") }}
                </div>
              </div>
              <div>
                <div class="text-caption text-muted">{{ t("usage.period.month") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink">{{ account.monthRequests }}</div>
                <div class="text-caption text-ink-soft">
                  {{ creditText(account.monthCredit) }} {{ t("usage.unitCredit") }}
                </div>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-2 rounded-control bg-line-soft px-3 py-2 text-caption">
              <div>
                <div class="text-muted">{{ t("usage.account.dailyAvg") }}</div>
                <div class="mt-0.5 font-medium text-ink">
                  {{ creditText(dailyAverage(account)) }}
                </div>
              </div>
              <div>
                <div class="text-muted">{{ t("usage.account.peakDay") }}</div>
                <div class="mt-0.5 font-medium text-ink">
                  <template v-if="peakDay(account)">
                    {{ shortDate(peakDay(account)?.date ?? "") }} ·
                    {{ creditText(peakDay(account)?.credit ?? 0) }}
                  </template>
                  <template v-else>—</template>
                </div>
              </div>
              <div class="col-span-2 text-micro leading-relaxed text-muted">
                {{ t("usage.account.projection", { value: creditText(dailyAverage(account) * 30) }) }}
              </div>
            </div>

            <div v-if="account.daily.length">
              <div class="flex items-end gap-[2px]" style="height: 44px">
                <div
                  v-for="day in account.daily"
                  :key="day.date"
                  class="flex h-full flex-1 items-end"
                  :title="
                    t('usage.dayTooltip', {
                      date: day.date,
                      credit: creditText(day.credit),
                      requests: day.requests,
                    })
                  "
                >
                  <div
                    class="w-full rounded-t-[2px] bg-brand"
                    :style="{
                      height: `${Math.max(
                        6,
                        ratio(day.credit, Math.max(0, ...account.daily.map((item) => item.credit))),
                      )}%`,
                    }"
                  />
                </div>
              </div>
              <div class="mt-1 flex justify-between text-micro text-muted">
                <span>{{ shortDate(account.daily[0]?.date ?? "") }}</span>
                <span>{{ shortDate(account.daily[account.daily.length - 1]?.date ?? "") }}</span>
              </div>
            </div>

            <div class="text-caption text-muted">
              {{
                account.detail.length
                  ? t("usage.account.linked", {
                      linked: account.linkedRequests,
                      total: account.detail.length,
                    })
                  : t("usage.account.noLinked")
              }}
            </div>
          </template>
        </article>
      </div>
    </div>

    <!-- ================= 分布 ================= -->
    <div v-else-if="view === 'distribution'" class="mt-4 space-y-4">
      <div class="grid gap-4 lg:grid-cols-2">
        <SectionCard :title="t('usage.dist.hourlyTitle')" :subtitle="t('usage.dist.hourlySubtitle')">
          <div v-if="heatMax > 0">
            <div
              class="grid gap-[3px]"
              style="grid-template-columns: 34px repeat(24, minmax(0, 1fr))"
            >
              <div />
              <div
                v-for="hour in 24"
                :key="`h-${hour}`"
                class="text-center text-micro leading-[16px] text-muted"
              >
                {{ (hour - 1) % 3 === 0 ? pad(hour - 1) : "" }}
              </div>
              <template v-for="(label, dayIndex) in weekdayLabels" :key="label">
                <div class="pr-1 text-right text-micro leading-[18px] text-muted">{{ label }}</div>
                <div
                  v-for="hour in 24"
                  :key="`${dayIndex}-${hour}`"
                  class="h-[18px] rounded-mark transition-opacity hover:opacity-80"
                  :style="heatStyle(dayIndex, hour - 1)"
                  :title="heatTitle(dayIndex, hour - 1)"
                />
              </template>
            </div>
            <div class="mt-2 flex items-center justify-end gap-2 text-micro text-muted">
              <span>{{ t("usage.dist.legendLow") }}</span>
              <span
                v-for="step in [18, 42, 66, 90]"
                :key="step"
                class="h-2.5 w-5 rounded-mark"
                :style="{
                  background: `color-mix(in srgb, var(--color-brand) ${step}%, var(--color-surface))`,
                }"
              />
              <span>{{ t("usage.dist.legendHigh") }}</span>
            </div>
          </div>
          <EmptyState v-else :title="t('usage.dist.hourlyEmpty')" />
        </SectionCard>

        <SectionCard :title="t('usage.dist.singleTitle')" :subtitle="t('usage.dist.singleSubtitle')">
          <template v-if="creditStats.count">
            <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div>
                <div class="text-caption text-muted">{{ t("usage.dist.avg") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink">
                  {{ creditText(creditStats.mean) }}
                </div>
              </div>
              <div>
                <div class="text-caption text-muted">{{ t("usage.dist.median") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink">
                  {{ creditText(creditStats.median) }}
                </div>
              </div>
              <div>
                <div class="text-caption text-muted">{{ t("usage.dist.p90") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-ink">
                  {{ creditText(creditStats.p90) }}
                </div>
              </div>
              <div>
                <div class="text-caption text-muted">{{ t("usage.dist.maxValue") }}</div>
                <div class="mt-0.5 text-metric font-semibold text-warn">
                  {{ creditText(creditStats.max) }}
                </div>
              </div>
            </div>
            <div class="mt-1 text-caption text-muted">
              {{ t("usage.dist.samples", { count: creditStats.count }) }}
            </div>

            <div class="mt-4 space-y-2">
              <div
                v-for="bucket in bucketStats"
                :key="bucket.label"
                class="flex items-center gap-3 text-caption"
              >
                <span class="w-28 shrink-0 text-muted">{{ bucket.label }}</span>
                <div class="h-2.5 flex-1 overflow-hidden rounded-full bg-line-soft">
                  <div
                    class="h-full rounded-full bg-brand"
                    :style="{ width: `${bucket.percent}%` }"
                  />
                </div>
                <span class="w-10 shrink-0 text-right text-ink-soft">{{ bucket.count }}</span>
              </div>
            </div>
          </template>
          <EmptyState v-else :title="t('usage.dist.empty')" />
        </SectionCard>
      </div>

      <div class="grid gap-4 lg:grid-cols-2">
        <SectionCard :title="t('usage.dist.clientTitle')" :subtitle="t('usage.dist.clientSubtitle')">
          <div v-if="clientAgg.length" class="space-y-2.5">
            <div
              v-for="item in clientAgg"
              :key="item.client"
              class="flex items-center gap-3 text-body-sm"
            >
              <span class="w-32 shrink-0 truncate text-ink-soft" :title="item.client">
                {{ item.client }}
              </span>
              <div class="h-2 flex-1 overflow-hidden rounded-full bg-line-soft">
                <div
                  class="h-full rounded-full bg-brand"
                  :style="{ width: `${ratio(item.credit, clientMax)}%` }"
                />
              </div>
              <span class="w-20 shrink-0 text-right text-muted">
                {{ creditText(item.credit) }} {{ t("usage.unitCredit") }}
              </span>
              <span class="w-14 shrink-0 text-right text-caption text-muted">
                {{ item.requests }} {{ t("usage.unitRequests") }}
              </span>
            </div>
          </div>
          <EmptyState v-else :title="t('usage.dist.clientEmpty')" />
        </SectionCard>

        <SectionCard
          :title="t('usage.dist.efficiencyTitle')"
          :subtitle="t('usage.dist.efficiencySubtitle')"
        >
          <div v-if="modelEfficiency.length" class="space-y-2.5">
            <div
              v-for="item in modelEfficiency"
              :key="item.model"
              class="flex items-center gap-3 text-body-sm"
            >
              <span class="w-40 shrink-0 truncate text-ink-soft" :title="item.model">
                {{ item.model }}
              </span>
              <div class="h-2 flex-1 overflow-hidden rounded-full bg-line-soft">
                <div
                  class="h-full rounded-full bg-warn"
                  :style="{ width: `${ratio(item.average, efficiencyMax)}%` }"
                />
              </div>
              <span class="w-20 shrink-0 text-right font-medium text-ink">
                {{ creditText(item.average) }}
                <span class="text-micro font-normal text-muted">{{ t("usage.dist.perRequest") }}</span>
              </span>
              <span class="w-14 shrink-0 text-right text-caption text-muted">
                {{ item.requests }} {{ t("usage.unitRequests") }}
              </span>
            </div>
          </div>
          <EmptyState v-else :title="t('usage.dist.efficiencyEmpty')" />
        </SectionCard>
      </div>

      <SectionCard :title="t('usage.dist.matrixTitle')" :subtitle="t('usage.dist.matrixSubtitle')" flush>
        <div v-if="matrixModels.length" class="overflow-x-auto">
          <table class="w-full min-w-[720px] text-body-sm">
            <thead>
              <tr class="border-b border-line-soft text-caption text-muted">
                <th class="px-4 py-2.5 text-left font-medium">{{ t("usage.detail.account") }}</th>
                <th
                  v-for="model in matrixModels"
                  :key="model"
                  class="px-2 py-2.5 text-center font-medium"
                  :title="model"
                >
                  <span class="inline-block max-w-[110px] truncate align-bottom">{{ model }}</span>
                </th>
                <th class="px-4 py-2.5 text-right font-medium">{{ t("usage.dist.rowTotal") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="account in workbuddyAccounts"
                :key="account.accountId"
                class="border-b border-line-soft last:border-b-0"
              >
                <td class="max-w-[180px] truncate px-4 py-2 text-ink">{{ account.displayName }}</td>
                <td v-for="model in matrixModels" :key="model" class="px-1 py-1.5 text-center">
                  <div
                    class="rounded-badge px-2 py-1.5 text-caption"
                    :class="matrixValue(account, model) === null ? 'text-muted' : 'text-ink'"
                    :style="matrixStyle(matrixValue(account, model))"
                    :title="`${account.displayName} · ${model}`"
                  >
                    {{
                      matrixValue(account, model) === null
                        ? "—"
                        : creditText(matrixValue(account, model))
                    }}
                  </div>
                </td>
                <td class="px-4 py-2 text-right font-medium text-ink">
                  {{ creditText(account.creditConsumed) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <EmptyState v-else :title="t('usage.dist.matrixEmpty')" />
      </SectionCard>

      <SectionCard :title="t('usage.dist.topTitle')" :subtitle="t('usage.detail.clickHint')" flush>
        <ul v-if="topRequests.length" class="divide-y divide-line-soft">
          <li v-for="(item, index) in topRequests" :key="item.row.requestId">
            <button
              type="button"
              class="flex w-full flex-col gap-1 px-4 py-2.5 text-left transition-colors hover:bg-line-soft"
              @click="openRequestTurn(item.account, item.row)"
            >
              <span class="flex w-full items-center gap-3 text-body-sm">
                <span class="w-6 shrink-0 text-caption text-muted">{{ index + 1 }}</span>
                <span class="w-32 shrink-0 truncate text-muted">{{ item.row.requestTime }}</span>
                <span class="w-40 shrink-0 truncate text-ink-soft">{{ item.row.model }}</span>
                <span class="min-w-0 flex-1 truncate text-muted">
                  {{ item.account.displayName }}
                </span>
                <span class="shrink-0 font-medium text-ink">
                  {{ creditText(item.row.credit) }} {{ t("usage.unitCredit") }}
                </span>
                <Flame :size="13" class="shrink-0 text-warn" />
              </span>
              <span class="block w-full pl-9 text-caption leading-relaxed">
                <template v-if="briefFor(item.account.accountId, item.row.requestId)">
                  <span class="block truncate text-ink-soft">
                    <span class="text-muted">{{ t("usage.detail.asked") }}</span>
                    {{ briefFor(item.account.accountId, item.row.requestId)?.userText || "—" }}
                  </span>
                  <span class="mt-0.5 block truncate text-muted">
                    {{ t("usage.detail.replied") }}
                    {{ briefFor(item.account.accountId, item.row.requestId)?.assistantText || "—" }}
                  </span>
                </template>
                <span v-else-if="briefLoading" class="text-caption text-muted">
                  <Loader2 :size="11" class="inline animate-spin" />
                </span>
                <span v-else class="text-caption text-muted">
                  {{ t("usage.detail.briefNotFound") }}
                </span>
              </span>
            </button>
          </li>
        </ul>
        <EmptyState v-else :title="t('usage.dist.empty')" />
      </SectionCard>
    </div>

    <!-- ================= 对话 ================= -->
    <div v-else-if="view === 'conversations'" class="mt-4 space-y-4">
      <SectionCard :title="t('usage.conv.title')" :subtitle="t('usage.conv.subtitle')" flush>
        <template #actions>
          <span class="text-caption text-muted">
            {{ t("usage.conv.linked", conversationLinked) }}
          </span>
        </template>

        <div class="flex items-center gap-2 px-4 pb-3">
          <SearchInput
            v-model="convQuery"
            :placeholder="t('usage.conv.search')"
            size="sm"
            class="w-full max-w-[360px]"
          />
        </div>

        <ul v-if="conversationRows.length" class="divide-y divide-line-soft border-t border-line-soft">
          <li
            v-for="item in conversationRows"
            :key="`${item.account.accountId}-${item.conversation.conversationId}`"
          >
            <button
              type="button"
              class="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-line-soft"
              @click="openConversation(item.account, item.conversation)"
            >
              <MessageSquare :size="15" class="shrink-0 text-muted" />
              <div class="min-w-0 flex-1">
                <div class="truncate text-body-sm font-medium text-ink">
                  {{ item.conversation.title || t("usage.conv.unknown") }}
                </div>
                <div class="mt-0.5 truncate text-caption text-muted">
                  {{ item.account.displayName }} ·
                  {{ workspaceName(item.conversation.cwd) }} ·
                  {{ t("usage.conv.requests", { count: item.conversation.requests }) }} ·
                  {{ t("usage.conv.last", { time: timeText(item.conversation.lastTime) }) }}
                </div>
              </div>
              <div class="shrink-0 text-right">
                <div class="text-body font-semibold text-ink">
                  {{ creditText(item.conversation.credit) }}
                  <span class="text-micro font-normal text-muted">{{ t("usage.unitCredit") }}</span>
                </div>
                <div class="mt-0.5 text-caption text-brand">{{ t("usage.conv.open") }}</div>
              </div>
            </button>
          </li>
        </ul>
        <EmptyState v-else :title="t('usage.conv.empty')" />
      </SectionCard>
    </div>

    <!-- ================= 对比 ================= -->
    <div v-else-if="view === 'compare'" class="mt-4">
      <SectionCard :title="t('usage.compare.title')" :subtitle="t('usage.compare.subtitle')" flush>
        <div class="overflow-x-auto">
          <table class="w-full min-w-[720px] text-body-sm">
            <thead>
              <tr class="border-b border-line-soft text-caption text-muted">
                <th class="px-5 py-2.5 text-left font-medium">{{ t("usage.stat.accounts") }}</th>
                <th
                  v-for="column in COMPARE_COLUMNS"
                  :key="column.key"
                  class="cursor-pointer px-4 py-2.5 text-right font-medium transition-colors hover:text-ink"
                  @click="toggleSort(column.key)"
                >
                  {{ t(column.labelKey) }}
                  <span v-if="sortKey === column.key">{{ sortDesc ? "↓" : "↑" }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in compareRows"
                :key="row.account.accountId"
                class="border-b border-line-soft last:border-b-0"
              >
                <td class="px-5 py-2.5">
                  <div class="flex items-center gap-2">
                    <span class="truncate text-ink">{{ row.account.displayName }}</span>
                    <span class="shrink-0 text-caption text-muted">
                      {{ PLATFORM_LABEL[row.account.platform] ?? row.account.platform }}
                    </span>
                    <StatusPill v-if="!row.account.detectable" tone="danger">
                      {{ t("usage.undetectable") }}
                    </StatusPill>
                  </div>
                  <div v-if="!row.account.detectable" class="mt-0.5 text-caption text-muted">
                    {{ row.account.error ?? t("usage.unsupportedCredential") }}
                  </div>
                </td>
                <td
                  v-for="column in COMPARE_COLUMNS"
                  :key="column.key"
                  class="whitespace-nowrap px-4 py-2.5 text-right"
                >
                  <template v-if="row.account.detectable">
                    <div class="text-ink">
                      {{ row.cells[column.key].requests === null ? "—" : row.cells[column.key].requests }}
                      <span class="text-caption font-normal text-muted">
                        {{ t("usage.unitRequests") }}
                      </span>
                    </div>
                    <div class="text-caption text-muted">
                      {{ creditText(row.cells[column.key].credit) }} {{ t("usage.unitCredit") }}
                    </div>
                  </template>
                  <span v-else class="text-muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="border-t border-line-soft px-5 py-2.5 text-caption text-muted">
          {{ t("usage.compare.footnote") }}
        </div>
      </SectionCard>
    </div>

    <!-- ================= 明细 ================= -->
    <div v-else class="mt-4">
      <SectionCard :title="t('usage.view.detail')" :subtitle="t('usage.detail.clickHint')" flush>
        <div class="flex flex-wrap items-center gap-2 px-4 pb-3">
          <SelectMenu
            v-model="detailAccount"
            :options="detailAccountOptions"
            :placeholder="t('usage.detail.filterAccount')"
            size="sm"
            class="w-[150px]"
          />
          <SelectMenu
            v-model="detailModel"
            :options="detailModelOptions"
            :placeholder="t('usage.detail.filterModel')"
            size="sm"
            class="w-[180px]"
          />
          <SelectMenu
            v-model="detailPeriod"
            :options="periodOptions"
            size="sm"
            class="w-[112px]"
          />
          <SelectMenu
            v-model="detailSort"
            :options="detailSortOptions"
            size="sm"
            class="w-[112px]"
          />
          <SearchInput
            v-model="detailKeyword"
            :placeholder="t('usage.detail.keyword')"
            size="sm"
            class="w-48"
          />
          <span class="ml-auto text-caption text-muted">
            {{ t("usage.detail.showing", { count: detailRows.length }) }}
          </span>
        </div>

        <div class="overflow-x-auto border-t border-line-soft">
          <table class="w-full min-w-[860px] text-body-sm">
            <thead>
              <tr class="border-b border-line-soft text-caption text-muted">
                <th class="px-4 py-2.5 text-left font-medium">{{ t("usage.detail.time") }}</th>
                <th class="px-4 py-2.5 text-left font-medium">{{ t("usage.detail.account") }}</th>
                <th class="px-4 py-2.5 text-left font-medium">{{ t("usage.detail.model") }}</th>
                <th class="px-4 py-2.5 text-left font-medium">{{ t("usage.detail.client") }}</th>
                <th class="px-4 py-2.5 text-left font-medium">{{ t("usage.detail.dialog") }}</th>
                <th class="px-4 py-2.5 text-right font-medium">{{ t("usage.detail.credit") }}</th>
                <th class="px-4 py-2.5 text-right font-medium">{{ t("usage.detail.hasDialog") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in pagedRows"
                :key="`${item.account.accountId}-${item.row.requestId}`"
                class="cursor-pointer border-b border-line-soft transition-colors last:border-b-0 hover:bg-line-soft"
                @click="openRequestTurn(item.account, item.row)"
              >
                <td class="whitespace-nowrap px-4 py-2 text-muted">{{ item.row.requestTime }}</td>
                <td class="max-w-[160px] truncate px-4 py-2 text-ink-soft">
                  {{ item.account.displayName }}
                </td>
                <td class="max-w-[160px] truncate px-4 py-2 text-ink">{{ item.row.model }}</td>
                <td class="max-w-[120px] truncate px-4 py-2 text-muted">{{ item.row.client }}</td>
                <td class="max-w-[300px] px-4 py-2 align-top">
                  <template v-if="briefFor(item.account.accountId, item.row.requestId)">
                    <div class="truncate text-caption text-ink-soft">
                      <span class="text-muted">{{ t("usage.detail.asked") }}</span>
                      {{ briefFor(item.account.accountId, item.row.requestId)?.userText || "—" }}
                    </div>
                    <div class="mt-0.5 truncate text-caption text-muted">
                      <span>{{ t("usage.detail.replied") }}</span>
                      {{ briefFor(item.account.accountId, item.row.requestId)?.assistantText || "—" }}
                    </div>
                  </template>
                  <span
                    v-else-if="briefLoading"
                    class="text-caption text-muted"
                  >
                    <Loader2 :size="11" class="inline animate-spin" />
                  </span>
                  <span v-else class="text-caption text-muted">
                    {{ t("usage.detail.briefNotFound") }}
                  </span>
                </td>
                <td class="whitespace-nowrap px-4 py-2 text-right text-ink">
                  {{ creditText(item.row.credit) }}
                </td>
                <td class="whitespace-nowrap px-4 py-2 text-right">
                  <span
                    v-if="briefFor(item.account.accountId, item.row.requestId)"
                    class="inline-flex items-center gap-1 text-caption text-brand"
                  >
                    <MessageSquare :size="12" /> {{ t("usage.detail.dialogCount", {
                      count: briefFor(item.account.accountId, item.row.requestId)?.messageCount ?? 0,
                    }) }}
                  </span>
                  <span v-else class="text-caption text-muted">{{ t("usage.detail.noDialog") }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <EmptyState v-if="!detailRows.length" :title="t('usage.detail.empty')" />

        <div
          v-if="detailRows.length"
          class="flex items-center justify-end gap-2 border-t border-line-soft px-4 py-2.5 text-caption text-muted"
        >
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-control border border-line px-2 py-1 transition-colors hover:text-ink disabled:opacity-40"
            :disabled="detailPage <= 1"
            @click="detailPage -= 1"
          >
            <ChevronLeft :size="12" /> {{ t("usage.detail.prev") }}
          </button>
          <span>{{ t("usage.detail.page", { page: detailPage, pages: detailPages }) }}</span>
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-control border border-line px-2 py-1 transition-colors hover:text-ink disabled:opacity-40"
            :disabled="detailPage >= detailPages"
            @click="detailPage += 1"
          >
            {{ t("usage.detail.next") }} <ChevronRight :size="12" />
          </button>
        </div>
      </SectionCard>
    </div>

    <!-- ================= 对话抽屉 ================= -->
    <div v-if="drawer" class="fixed inset-0 z-[var(--z-modal)] flex justify-end">
      <div class="absolute inset-0 bg-black/25" @click="closeDrawer" />
      <aside class="relative flex h-full w-full max-w-[620px] flex-col border-l border-line bg-canvas shadow-xl">
        <header class="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div class="min-w-0">
            <div class="truncate text-title font-semibold text-ink">{{ drawer.title }}</div>
            <div class="mt-1 flex flex-wrap items-center gap-2 text-caption text-muted">
              <span>{{ drawer.subtitle }}</span>
              <StatusPill v-if="drawer.messages.length" tone="neutral">
                {{ t("usage.detail.messageCount", { count: drawer.messages.length }) }}
              </StatusPill>
              <StatusPill v-if="drawer.kind === 'request'" tone="neutral">
                {{ t("usage.drawer.requestTitle") }}
              </StatusPill>
              <StatusPill v-else tone="neutral">
                {{ t("usage.drawer.conversationTitle") }}
              </StatusPill>
            </div>
          </div>
          <button
            type="button"
            class="rounded-control p-1.5 text-muted transition-colors hover:bg-line-soft hover:text-ink"
            :aria-label="t('usage.drawer.close')"
            @click="closeDrawer"
          >
            <X :size="16" />
          </button>
        </header>

        <div class="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div v-if="drawer.meta.length" class="mb-4 grid grid-cols-2 gap-2 text-caption">
            <div v-for="item in drawer.meta" :key="item.label" class="flex items-baseline gap-1">
              <span class="font-medium text-muted">{{ item.label }}</span>
              <span class="text-ink-soft">{{ item.value }}</span>
            </div>
          </div>

          <div
            v-if="drawer.note"
            class="mb-3 rounded-control border border-line bg-surface px-3 py-2 text-caption text-muted"
          >
            {{ drawer.note }}
          </div>

          <div v-if="drawer.loading" class="flex items-center gap-2 py-10 text-body text-muted">
            <Loader2 :size="15" class="animate-spin" /> {{ t("usage.drawer.loading") }}
          </div>

          <div
            v-else-if="drawer.notFound"
            class="flex items-start gap-2.5 rounded-card border border-line bg-surface px-4 py-3 text-body-sm text-ink-soft"
          >
            <AlertTriangle :size="15" class="mt-0.5 shrink-0 text-warn" />
            <div>
              <div class="font-medium text-ink">{{ t("usage.drawer.notFound") }}</div>
              <div class="mt-0.5 leading-relaxed text-muted">
                {{ drawer.error ?? t("usage.drawer.notFoundHint") }}
              </div>
            </div>
          </div>

          <div
            v-else-if="drawer.error"
            class="rounded-card border border-line bg-danger-soft px-4 py-3 text-body-sm text-danger"
          >
            {{ t("usage.drawer.error", { message: drawer.error }) }}
          </div>

          <EmptyState v-else-if="!drawer.messages.length" :title="t('usage.drawer.empty')" />

          <template v-else>
            <div
              v-if="drawer.messages.length > MAX_DRAWER_MESSAGES"
              class="mb-3 rounded-control border border-line bg-surface px-3 py-2 text-caption text-muted"
            >
              {{ t("usage.drawer.truncated", { count: shownDrawerMessages.length }) }}
            </div>
            <div class="space-y-4">
              <div
                v-for="(message, index) in shownDrawerMessages"
                :key="index"
                class="flex flex-col"
                :class="message.role === 'user' ? 'items-end' : 'items-start'"
              >
                <div class="mb-1 flex items-center gap-2 text-caption text-muted">
                  <span>
                    {{ message.role === "user" ? t("usage.detail.roleUser") : t("usage.detail.roleAssistant") }}
                  </span>
                  <span v-if="message.role === 'assistant' && message.model">{{ message.model }}</span>
                  <span v-if="message.at">{{ formatTime(isoOf(message.at)) }}</span>
                </div>
                <div
                  class="max-w-[88%] whitespace-pre-wrap break-words rounded-card px-3.5 py-2.5 text-body-sm leading-relaxed"
                  :class="
                    message.role === 'user'
                      ? 'bg-brand-soft text-ink'
                      : 'border border-line bg-surface text-ink-soft'
                  "
                >
                  {{ message.text }}
                </div>
              </div>
            </div>
          </template>
        </div>
      </aside>
    </div>
  </div>
</template>
