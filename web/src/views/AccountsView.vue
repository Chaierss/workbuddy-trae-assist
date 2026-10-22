<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  AlertTriangle,
  CheckSquare,
  ClipboardPaste,
  Coins,
  Download,
  FileUp,
  History,
  Loader2,
  MinusSquare,
  QrCode,
  RotateCcw,
  Search,
  Shuffle,
  Square,
  Trash2,
  UserPlus,
  Wallet,
  X,
} from "lucide-vue-next";
import AccountCard from "../components/AccountCard.vue";
import AnimatedNumber from "../components/AnimatedNumber.vue";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import NoteDialog from "../components/NoteDialog.vue";
import OAuthLoginDialog from "../components/OAuthLoginDialog.vue";
import SelectMenu from "../components/SelectMenu.vue";
import SwitchDialog from "../components/SwitchDialog.vue";
import {
  api,
  formatAmount,
  type Account,
  type ActivityBrief,
  type ClientSlot,
  type CredentialHealth,
  type MigrationPreview,
  type OverviewPulse,
  type RecycleBinAccount,
  type SwitchReport,
} from "../api";
import {
  accounts,
  attentionCount,
  clientDetections,
  credits,
  creditsLoading,
  detection,
  loadCredits,
  loading,
  refreshAll,
  switchBlockedReason,
} from "../stores/app";
import { localeTag, t } from "../lib/i18n";

const keyword = ref("");
const sortBy = ref<"recent" | "name" | "credits" | "expiry">("recent");
const platformFilter = ref<"all" | "workbuddy" | "trae">("all");
const importing = ref(false);
const exporting = ref(false);
const toast = ref<string | null>(null);

/**
 * 筛选口径：点顶部健康度卡片或卡片上的风险徽标即可筛出同一类账号。
 * - 风险（relogin / expiring / low）：只在有值时筛，没值禁用整块点击；
 * - 正面口径（usable / today）：筛可用账号 / 有活动余量的账号；
 * - total 是「号池总数」专用：点它不是筛，而是清空筛选回到全部。
 */
type RiskKey = "" | "relogin" | "expiring" | "low" | "usable" | "today";
type FilterKey = RiskKey | "total";
const riskFilter = ref<RiskKey>("");

/** 低积分阈值：低于它算「快用完了」，只做提醒不做判断依据。 */
const LOW_CREDITS = 100;

/** 列表分页：账号多时不再一次渲染全部卡片。 */
const PAGE_SIZE = 24;
const pageSize = ref(PAGE_SIZE);

/** 今日待做 / 网关等汇总来自概览接口，取不到就整块不显示，不编数字。 */
const pulse = ref<OverviewPulse | null>(null);

const VIEW_KEY = "chaierww.pool-view";

interface PoolViewPrefs {
  keyword: string;
  sortBy: "recent" | "name" | "credits" | "expiry";
  platform: "all" | "workbuddy" | "trae";
  risk: RiskKey;
}

/** 读回上次的筛选/排序；坏数据直接忽略，不让它把页面卡住。 */
function readViewPrefs(): void {
  try {
    const raw = localStorage.getItem(VIEW_KEY);
    if (!raw) return;
    const data = JSON.parse(raw) as Partial<PoolViewPrefs>;
    if (typeof data.keyword === "string") keyword.value = data.keyword;
    if (["recent", "name", "credits", "expiry"].includes(data.sortBy ?? "")) {
      sortBy.value = data.sortBy as PoolViewPrefs["sortBy"];
    }
    if (["all", "workbuddy", "trae"].includes(data.platform ?? "")) {
      platformFilter.value = data.platform as PoolViewPrefs["platform"];
    }
    if (["", "relogin", "expiring", "low", "usable", "today"].includes(data.risk ?? "")) {
      riskFilter.value = (data.risk ?? "") as RiskKey;
    }
  } catch {
    localStorage.removeItem(VIEW_KEY);
  }
}

readViewPrefs();

// URL 上的 ?risk= 预置筛选优先级高于 localStorage：概览等页面点「凭据异常」跳过来
// 时带上参数，号池页直接落到对应筛选，不用再手点一次。合法值才认，非法忽略。
const route = useRoute();
const RISK_KEYS: RiskKey[] = ["relogin", "expiring", "low", "usable", "today"];
if (typeof route.query.risk === "string" && (RISK_KEYS as string[]).includes(route.query.risk)) {
  riskFilter.value = route.query.risk as RiskKey;
}

watch([keyword, sortBy, platformFilter, riskFilter], () => {
  pageSize.value = PAGE_SIZE;
  localStorage.setItem(
    VIEW_KEY,
    JSON.stringify({
      keyword: keyword.value,
      sortBy: sortBy.value,
      platform: platformFilter.value,
      risk: riskFilter.value,
    } satisfies PoolViewPrefs),
  );
});

const jsonDialog = reactive({
  open: false,
  busy: false,
  content: "",
  fileName: "",
  error: "",
});

const oauthOpen = ref(false);
const oauthPlatform = ref<"workbuddy" | "trae">("workbuddy");

/** 多选：批量刷新积分 / 批量删除。 */
const selectedIds = ref<Set<string>>(new Set());
const selectedCount = computed(() => selectedIds.value.size);

function toggleSelect(account: Account): void {
  const next = new Set(selectedIds.value);
  if (next.has(account.id)) next.delete(account.id);
  else next.add(account.id);
  selectedIds.value = next;
}

function clearSelection(): void {
  selectedIds.value = new Set();
}

/** 备注编辑对话框。 */
const noteDialog = reactive({
  open: false,
  busy: false,
  account: null as Account | null,
  value: "",
});

function openNote(account: Account): void {
  noteDialog.account = account;
  noteDialog.value = account.note;
  noteDialog.open = true;
}

async function saveNote(note: string): Promise<void> {
  const account = noteDialog.account;
  if (!account) return;
  noteDialog.busy = true;
  try {
    await api.updateNote(account.id, note);
    noteDialog.open = false;
    showToast(t("accounts.noteSaved"));
    await refreshAll();
  } catch (error) {
    showToast((error as Error).message);
  } finally {
    noteDialog.busy = false;
  }
}

function askBatchDelete(): void {
  if (!selectedIds.value.size) return;
  confirm.account = null;
  confirm.mode = "batch-delete";
  confirm.open = true;
}

const fileInputRef = ref<HTMLInputElement | null>(null);

function pickFile(): void {
  fileInputRef.value?.click();
}

async function onFileChosen(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  // 清掉 value，让用户选同一个文件也能再次触发 change。
  input.value = "";
  await loadFile(file);
}

async function loadFile(file: File): Promise<void> {
  jsonDialog.error = "";
  // 登录态文件可能很大（storage.json 数十 KB、info 文件数百字节），
  // 这里只做一个宽松上限，超出即报错，避免塞爆内存。
  if (file.size > 5 * 1024 * 1024) {
    jsonDialog.error = t("accounts.fileTooLarge");
    return;
  }
  try {
    const text = await file.text();
    if (!text.trim()) {
      jsonDialog.error = t("accounts.fileEmpty");
      return;
    }
    jsonDialog.content = text;
    jsonDialog.fileName = file.name;
  } catch {
    jsonDialog.error = t("accounts.fileReadFailed");
  }
}

function onFileDrop(event: DragEvent): void {
  const file = event.dataTransfer?.files?.[0];
  if (file) void loadFile(file);
}

function onDragOver(event: DragEvent): void {
  event.preventDefault();
}

function openJsonImport(): void {
  jsonDialog.content = "";
  jsonDialog.fileName = "";
  jsonDialog.error = "";
  jsonDialog.open = true;
}

const dialog = reactive({
  open: false,
  busy: false,
  report: null as SwitchReport | null,
  error: null as string | null,
  /** 迁移预检结果（确认态）。 */
  preview: null as MigrationPreview | null,
  /** true = 处于「预检确认」态，尚未真正切换。 */
  confirming: false,
  /** 待切换的账号（确认态需要它来执行切换）。 */
  account: null as Account | null,
  /** 目标账号所属平台登记的程序位（只读 / 未适配的位置会灰掉）。 */
  slots: [] as ClientSlot[],
  /** 选中的程序位 key；空串 = 该平台不区分程序位，按默认位切换。 */
  slot: "",
});

const confirm = reactive({
  open: false,
  busy: false,
  mode: "" as "" | "delete" | "batch-delete" | "export",
  account: null as Account | null,
});

/* ---------- 回收站 ---------- */

/**
 * 删除是软删除：确认框里写了「保留 30 天，期间可以恢复」，
 * 那这里就必须真有一个能点进去的入口，否则那句承诺等于空话。
 */
const recycle = reactive({
  open: false,
  busy: false,
  loaded: false,
  error: "",
  items: [] as RecycleBinAccount[],
});

async function loadRecycleBin(): Promise<void> {
  recycle.busy = true;
  recycle.error = "";
  try {
    const body = await api.recycleBin();
    recycle.items = body.accounts;
    recycle.loaded = true;
  } catch (error) {
    recycle.error = (error as Error).message;
  } finally {
    recycle.busy = false;
  }
}

function openRecycle(): void {
  recycle.open = true;
  void loadRecycleBin();
}

/** 恢复一个账号：凭据没被删过，所以恢复后可以直接切号，不必重新扫码。 */
async function restoreFromRecycle(item: RecycleBinAccount): Promise<void> {
  recycle.busy = true;
  try {
    await api.restoreAccount(item.id);
    showToast(t("accounts.recycle.restored", { name: item.displayName }));
    await Promise.all([loadRecycleBin(), refreshAll()]);
  } catch (error) {
    showToast((error as Error).message);
  } finally {
    recycle.busy = false;
  }
}

/* ---------- 凭据体检 ---------- */

/**
 * 只报「缺字段」的账号（目前是缺 refresh token）。
 * 这类账号能切号、能导出，但 token 一过期就要重新扫码 —— 早说比晚说好。
 */
const credentialHealth = ref<CredentialHealth | null>(null);

async function loadCredentialHealth(): Promise<void> {
  try {
    credentialHealth.value = await api.credentialHealth();
  } catch {
    // 体检失败不影响号池，顶多不显示这条提示。
    credentialHealth.value = null;
  }
}

const gapAccounts = computed(() => credentialHealth.value?.accounts ?? []);

/** 缺口账号的名字，最多列 3 个，剩下的折成「等 N 个」。 */
const gapNames = computed(() => {
  const names = gapAccounts.value.map((item) => item.displayName);
  if (names.length <= 3) return names.join("、");
  return `${names.slice(0, 3).join("、")} 等 ${names.length} 个`;
});

/** 选中项里凭据残缺的个数：导出确认里点明它，免得用户以为导出的是完整登录态。 */
const selectedGapCount = computed(() => {
  if (!selectedIds.value.size) return 0;
  return gapAccounts.value.filter((item) => selectedIds.value.has(item.accountId)).length;
});

const visible = computed(() => {
  const key = keyword.value.trim().toLowerCase();
  let list = accounts.value;

  if (platformFilter.value !== "all") {
    list = list.filter((item) => item.platform === platformFilter.value);
  }

  if (riskFilter.value === "relogin") {
    list = list.filter((item) => item.credentialState !== "usable");
  } else if (riskFilter.value === "usable") {
    list = list.filter((item) => item.credentialState === "usable");
  } else if (riskFilter.value === "expiring") {
    list = list.filter((item) => credits.value[item.id]?.expiringSoon === true);
  } else if (riskFilter.value === "low") {
    list = list.filter((item) => {
      const info = credits.value[item.id];
      if (!info?.available || info.error || info.credits === null) return false;
      return info.credits < LOW_CREDITS;
    });
  } else if (riskFilter.value === "today") {
    // 「今日待做」= 还有可自动 / 需手动做的活动。检测结果没拉到（activityBriefs 空）
    // 时这条筛选等于空集，而不是把所有账号都放出来 —— 宁可显示空态也不误导。
    list = list.filter((item) => {
      const brief = activityBriefs.value[item.id];
      if (!brief) return false;
      return brief.automated > 0 || brief.manual > 0;
    });
  }

  if (key) {
    list = list.filter(
      (item) =>
        item.displayName.toLowerCase().includes(key) ||
        item.note.toLowerCase().includes(key) ||
        item.externalId.toLowerCase().includes(key) ||
        item.platform.toLowerCase().includes(key) ||
        platformLabel(item.platform).toLowerCase().includes(key),
    );
  }

  const creditOf = (id: string) => credits.value[id];

  return [...list].sort((a, b) => {
    if (sortBy.value === "name") return a.displayName.localeCompare(b.displayName, localeTag());

    if (sortBy.value === "credits") {
      // 能查到积分的排前面（降序）；查不到的（null/不可用）沉底。
      const left = creditOf(a.id);
      const right = creditOf(b.id);
      const lv = left?.available && left.credits !== null ? left.credits : -1;
      const rv = right?.available && right.credits !== null ? right.credits : -1;
      return rv - lv;
    }

    if (sortBy.value === "expiry") {
      // 到期最近的排前面；无到期日的沉底。
      const left = creditOf(a.id);
      const right = creditOf(b.id);
      const lv = left?.available && left.expiresAt ? left.expiresAt : Number.MAX_SAFE_INTEGER;
      const rv = right?.available && right.expiresAt ? right.expiresAt : Number.MAX_SAFE_INTEGER;
      return lv - rv;
    }

    const left = a.lastUsedAt ? Date.parse(a.lastUsedAt) : 0;
    const right = b.lastUsedAt ? Date.parse(b.lastUsedAt) : 0;
    return right - left;
  });
});

function platformLabel(platform: string): string {
  if (platform === "trae") return "Trae";
  if (platform === "workbuddy") return "WorkBuddy";
  return platform;
}

/** 分页后的卡片：筛选/排序变化时回到第一页。 */
const shown = computed(() => visible.value.slice(0, pageSize.value));
const hasMore = computed(() => visible.value.length > pageSize.value);
function loadMore(): void {
  pageSize.value += PAGE_SIZE;
}

/**
 * 全选 = 选中「当前筛选结果」的全部账号，而不是只选已渲染的那一页。
 * 这样按平台/风险筛完之后，批量操作才不会漏掉没滚到的卡片。
 */
const allSelected = computed(
  () => visible.value.length > 0 && visible.value.every((item) => selectedIds.value.has(item.id)),
);

/** 半选：筛选结果里选了一部分。全选按钮据此显示「－」，否则只有勾/不勾两态看不出中间态。 */
const someSelected = computed(
  () => !allSelected.value && visible.value.some((item) => selectedIds.value.has(item.id)),
);

/**
 * 选中项里落在「当前筛选结果之外」的个数。
 *
 * 筛选/搜索变化时不清空选择（静默丢弃用户勾的号比留着更糟），但批量删除会连它们一起删，
 * 所以这个数字必须显式报出来，让用户自己决定要不要收敛。
 */
const hiddenSelectedCount = computed(() => {
  if (!selectedIds.value.size) return 0;
  const inView = new Set(visible.value.map((item) => item.id));
  let hidden = 0;
  for (const id of selectedIds.value) {
    if (!inView.has(id)) hidden += 1;
  }
  return hidden;
});

/** 全选只增减当前筛选结果，不动被筛掉的选择。 */
function toggleSelectAll(): void {
  const next = new Set(selectedIds.value);
  if (allSelected.value) {
    for (const item of visible.value) next.delete(item.id);
  } else {
    for (const item of visible.value) next.add(item.id);
  }
  selectedIds.value = next;
}

/** 反选：只翻当前筛选结果里的项，被筛掉的不动。 */
function invertSelection(): void {
  const next = new Set(selectedIds.value);
  for (const item of visible.value) {
    if (next.has(item.id)) next.delete(item.id);
    else next.add(item.id);
  }
  selectedIds.value = next;
}

/** 把选择收敛到当前筛选结果：要批量删除前用它排掉看不见的账号，避免误删。 */
function keepVisibleOnly(): void {
  const next = new Set<string>();
  for (const item of visible.value) {
    if (selectedIds.value.has(item.id)) next.add(item.id);
  }
  selectedIds.value = next;
}

/** Esc 退出选择；弹层开着时优先关弹层，不抢它的 Esc。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  // 回收站是普通弹层，Esc 直接关它，别顺手把底下的选择也清了。
  if (recycle.open) {
    recycle.open = false;
    return;
  }
  if (!selectedIds.value.size) return;
  if (jsonDialog.open || dialog.open || confirm.open || noteDialog.open || oauthOpen.value) return;
  clearSelection();
}

/** 顶部「号池健康度」：只用本地已有数据，不额外打上游。 */
interface HealthStat {
  key: string;
  label: string;
  value: number;
  hint: string;
  tone: "neutral" | "brand" | "warn" | "danger";
  risk?: FilterKey;
}

/** 数值配色：只按语义走，异常才着色，正常一律墨色。 */
const TONE_TEXT: Record<HealthStat["tone"], string> = {
  neutral: "text-ink",
  brand: "text-brand",
  warn: "text-warn",
  danger: "text-danger",
};

const health = computed<HealthStat[]>(() => {
  const usable = accounts.value.filter((item) => item.credentialState === "usable").length;
  const relogin = accounts.value.length - usable;
  const expiring = Object.values(credits.value).filter((item) => item.expiringSoon).length;
  const low = accounts.value.filter((item) => {
    const info = credits.value[item.id];
    if (!info?.available || info.error || info.credits === null) return false;
    return info.credits < LOW_CREDITS;
  }).length;
  const stats: HealthStat[] = [
    {
      key: "total",
      label: t("accounts.health.total"),
      value: accounts.value.length,
      hint: t("accounts.health.totalHint"),
      tone: "neutral",
      // 「号池总数」永远可点：筛选中时点它 = 清空筛选回到全部。
      risk: "total",
    },
    {
      key: "usable",
      label: t("accounts.health.usable"),
      value: usable,
      hint: t("accounts.health.usableHint"),
      tone: "brand",
      risk: "usable",
    },
    {
      key: "relogin",
      label: t("accounts.health.relogin"),
      value: relogin,
      hint: t("accounts.health.reloginHint"),
      tone: relogin ? "warn" : "neutral",
      risk: relogin ? "relogin" : undefined,
    },
    {
      key: "expiring",
      label: t("accounts.health.expiring"),
      value: expiring,
      hint: t("accounts.health.expiringHint"),
      tone: expiring ? "danger" : "neutral",
      risk: expiring ? "expiring" : undefined,
    },
    {
      key: "low",
      label: t("accounts.health.low"),
      value: low,
      hint: t("accounts.health.lowHint", { threshold: LOW_CREDITS }),
      tone: low ? "warn" : "neutral",
      risk: low ? "low" : undefined,
    },
    {
      key: "today",
      label: t("accounts.health.today"),
      value: pulse.value?.activityActionable ?? 0,
      hint: t("accounts.health.todayHint"),
      tone: (pulse.value?.activityActionable ?? 0) ? "brand" : "neutral",
      risk: "today",
    },
  ];
  return stats;
});

function applyRisk(risk?: FilterKey): void {
  if (!risk) return;
  // 「号池总数」是清空筛选，不是筛「全部」：直接回空，不做 toggle。
  if (risk === "total") {
    riskFilter.value = "";
    return;
  }
  riskFilter.value = riskFilter.value === risk ? "" : risk;
}

const riskLabel = computed(() => {
  if (riskFilter.value === "relogin") return t("accounts.health.relogin");
  if (riskFilter.value === "expiring") return t("accounts.health.expiring");
  if (riskFilter.value === "low") return t("accounts.health.low");
  if (riskFilter.value === "usable") return t("accounts.health.usable");
  if (riskFilter.value === "today") return t("accounts.health.today");
  return "";
});

/** 单账号的切换迁移预检：只在用户悬停/聚焦卡片时按需拉一次，缓存到本次会话。 */
const previews = ref<Record<string, MigrationPreview | "loading" | "none">>({});

async function loadPreview(account: Account): Promise<void> {
  if (account.platform !== "workbuddy") return;
  // 当前登录的账号就是源账号，预检只会回「源账号与目标账号相同」，没必要请求。
  if (isCurrent(account)) return;
  if (previews.value[account.id]) return;
  previews.value = { ...previews.value, [account.id]: "loading" };
  try {
    const preview = await api.migrationPreview(account.id, account.scope);
    previews.value = { ...previews.value, [account.id]: preview };
  } catch {
    // 预检失败只是少一条参考信息，不打扰用户，也不冒充「无需迁移」。
    previews.value = { ...previews.value, [account.id]: "none" };
  }
}

/**
 * 卡片上的活动余量：done / 可自动做的总数，外加仍需手动做的条数。
 * 「可自动做」= 能自动且还没完成，与活动页的「一键执行」口径一致。
 */
const activityBriefs = ref<Record<string, ActivityBrief>>({});

/** 拉每个账号的活动余量：活动页与号池共用同一份检测结果（带缓存）。 */
async function loadActivityBrief(ids?: string[], refresh = false): Promise<void> {
  try {
    const data = await api.activities(refresh, ids);
    const next: Record<string, ActivityBrief> = { ...activityBriefs.value };
    for (const account of data.accounts) {
      let done = 0;
      let automated = 0;
      let manual = 0;
      const states: ActivityBrief["states"] = [];
      for (const item of account.activities) {
        if (item.state === "done") {
          done += 1;
          states.push("done");
          continue;
        }
        if (item.automatable) {
          automated += 1;
          states.push("auto");
        } else if (item.state !== "unavailable") {
          manual += 1;
          states.push("manual");
        } else {
          states.push("unknown");
        }
      }
      next[account.accountId] = {
        total: account.activities.length,
        done,
        automated,
        manual,
        states,
      };
    }
    activityBriefs.value = next;
  } catch {
    // 活动检测失败不影响号池本身，卡片上不显示这一块即可。
  }
}

/** 平台筛选的可见性：只显示号池里真实存在的平台。 */
const platformOptions = computed(() => {
  const present = new Set(accounts.value.map((item) => item.platform));
  const options: Array<{ value: "all" | "workbuddy" | "trae"; label: string }> = [
    { value: "all", label: t("accounts.platformAll") },
  ];
  if (present.has("workbuddy")) options.push({ value: "workbuddy", label: "WorkBuddy" });
  if (present.has("trae")) options.push({ value: "trae", label: "Trae" });
  return options;
});

const subtitle = computed(() => {
  const parts = [t("shell.accountCount", { count: accounts.value.length })];
  if (attentionCount.value) parts.push(t("accounts.attentionCount", { count: attentionCount.value }));
  if (detection.value?.running) parts.push(t("accounts.clientRunning"));
  const total = totalCredits.value;
  if (total !== null) parts.push(t("accounts.totalCredits", { total }));
  return parts.join(" · ");
});

/** 号池积分合计。只累加「能查到」的账号；一个都查不到返回 null（不显示 0）。 */
const totalCredits = computed(() => {
  const values = Object.values(credits.value)
    .filter((item) => item.available && !item.error && item.credits !== null)
    .map((item) => item.credits as number);
  if (!values.length) return null;
  const sum = values.reduce((acc, value) => acc + value, 0);
  return formatAmount(sum);
});

/** 客户端扫描：已登录但不在号池 → 提示入池（按平台分别判定）。 */
const pendingPool = computed(() => {
  const clients = clientDetections.value.filter(
    (item) => item.loggedIn && item.currentAccountId,
  );
  return clients.some(
    (client) => !accounts.value.some((item) => item.externalId === client.currentAccountId),
  );
});

/**
 * 自动刷新频率：号池页内配置，存本地偏好（localStorage）。
 * 刷新内容 = 号池数据 + 客户端检测（积分/用量等 M2 数据源就绪后自动并入 refreshAll）。
 */
const REFRESH_KEY = "chaierww.pool-refresh-seconds";
const refreshOptions = computed(() => [
  { value: 0, label: t("accounts.refreshOff") },
  { value: 30, label: t("accounts.refresh30") },
  { value: 60, label: t("accounts.refresh60") },
  { value: 300, label: t("accounts.refresh300") },
]);
const refreshSeconds = ref(Number(localStorage.getItem(REFRESH_KEY) ?? 0));
const sortOptions = computed<Array<{ value: "recent" | "credits" | "expiry" | "name"; label: string }>>(
  () => [
    { value: "recent", label: t("accounts.sort.recent") },
    { value: "credits", label: t("accounts.sort.credits") },
    { value: "expiry", label: t("accounts.sort.expiry") },
    { value: "name", label: t("accounts.sort.name") },
  ],
);
let refreshTimer: number | null = null;

function applyRefreshTimer(): void {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (refreshSeconds.value > 0) {
    refreshTimer = window.setInterval(() => {
      if (!importing.value && !jsonDialog.busy) {
        void refreshAll();
        // 活动状态也一起刷（走服务端 2 分钟缓存，不在每次轮询都打上游）。
        void loadActivityBrief();
      }
    }, refreshSeconds.value * 1000);
  }
}

watch(refreshSeconds, (value) => {
  localStorage.setItem(REFRESH_KEY, String(value));
  applyRefreshTimer();
});

async function loadPulse(): Promise<void> {
  try {
    const data = await api.overviewActionables();
    pulse.value = data.pulse;
  } catch {
    pulse.value = null;
  }
}

onMounted(() => {
  applyRefreshTimer();
  window.addEventListener("keydown", onKeydown);
});
onMounted(() => {
  void loadPulse();
  void loadActivityBrief();
  // 回收站只在打开弹层时才需要明细，但按钮上的角标要有数，所以启动时拉一次。
  void loadRecycleBin();
  void loadCredentialHealth();
});
onBeforeUnmount(() => {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
  window.removeEventListener("keydown", onKeydown);
});

function isCurrent(account: Account): boolean {
  // 按平台匹配各自的当前登录账号：WorkBuddy / Trae 是独立客户端，
  // 各自有独立的 currentAccountId，不能用一个全局 detection 判断。
  const client = clientDetections.value.find((item) => item.platform === account.platform);
  return client?.currentAccountId === account.externalId;
}

function showToast(message: string): void {
  toast.value = message;
  window.clearTimeout((showToast as unknown as { timer?: number }).timer);
  (showToast as unknown as { timer?: number }).timer = window.setTimeout(() => {
    toast.value = null;
  }, 2400);
}

/**
 * 导出前先确认一次。
 *
 * 导出物里是**解密后的明文登录凭据**，拿到文件等于拿到这些账号，
 * 落到桌面/网盘/聊天附件都可能泄出去 —— 这一步不能只在 tooltip 里提一句。
 */
function askExport(): void {
  if (!selectedIds.value.size) return;
  confirm.account = null;
  confirm.mode = "export";
  confirm.open = true;
}

/** 导出选中的账号（含登录凭据）为 JSON 文件。 */
async function exportSelected(): Promise<void> {
  const ids = [...selectedIds.value];
  if (!ids.length) return;
  exporting.value = true;
  try {
    const bundle = await api.exportAccounts(ids);
    const stamp = new Date();
    const pad = (value: number) => String(value).padStart(2, "0");
    const name =
      `chaierww-accounts-${stamp.getFullYear()}${pad(stamp.getMonth() + 1)}` +
      `${pad(stamp.getDate())}-${pad(stamp.getHours())}${pad(stamp.getMinutes())}.json`;
    const blob = new Blob([JSON.stringify(bundle, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    anchor.click();
    URL.revokeObjectURL(url);
    showToast(
      bundle.skipped
        ? t("accounts.exportPartial", { count: bundle.accounts.length, skipped: bundle.skipped })
        : t("accounts.exportDone", { count: bundle.accounts.length }),
    );
  } catch (error) {
    showToast((error as Error).message);
  } finally {
    exporting.value = false;
  }
}

/** 判断粘贴/拖入的内容是不是本工具的账号导出包（而不是单个登录态 JSON）。 */
function looksLikeAccountBundle(content: string): boolean {
  try {
    const parsed = JSON.parse(content) as { format?: unknown };
    return parsed?.format === "chaierww.accounts";
  } catch {
    return false;
  }
}

/**
 * 认出导出包后预告会恢复什么。
 *
 * 「导入后账号显示未验证」很容易被读成「凭据没导进来」，所以在导入之前就把
 * 「这是号池导出包 / 含 N 个账号的登录凭据」说清楚，别等用户猜。
 *
 * `gapCount` 是包里**自带**的缺口标记（导出时就写进每条 `refreshTokenMissing`）。
 * 有了它，「导入后还是要重新登录」这件事在导入前就能被看见，不用等用户自己撞上。
 */
const detectedBundle = computed(() => {
  const content = jsonDialog.content.trim();
  if (!content || !looksLikeAccountBundle(content)) return null;
  try {
    const parsed = JSON.parse(content) as { accounts?: unknown };
    if (!Array.isArray(parsed.accounts)) return null;
    const entries = parsed.accounts as Array<{ refreshTokenMissing?: unknown }>;
    if (!entries.length) return null;
    const gapCount = entries.filter((item) => item?.refreshTokenMissing === true).length;
    return { count: entries.length, gapCount };
  } catch {
    return null;
  }
});

async function onImport(): Promise<void> {
  importing.value = true;
  try {
    // 统一号池：点「导入」就是导入当前登录的号，平台由后端自动探测。
    const result = await api.importAccount();
    showToast(result.created ? t("accounts.importedNew") : result.restored ? t("accounts.importedRestored") : t("accounts.importedUpdated"));
    await refreshAll();
    void loadCredentialHealth();
  } catch (error) {
    showToast((error as Error).message);
  } finally {
    importing.value = false;
  }
}

async function onJsonImport(): Promise<void> {
  const content = jsonDialog.content.trim();
  if (!content) return;
  jsonDialog.busy = true;
  try {
    if (looksLikeAccountBundle(content)) {
      // 导出包整包恢复：账号标识相同的会被覆盖凭据，新增的落进号池。
      const result = await api.importAccountBundle(content);
      jsonDialog.open = false;
      showToast(
        result.failed
          ? t("accounts.importBundlePartial", {
              imported: result.imported,
              failed: result.failed,
            })
          : t("accounts.importBundleDone", { imported: result.imported }),
      );
      await refreshAll();
      // 新导进来的号凭据是否完整，只有体检接口知道；立刻刷一次别让用户等。
      void loadCredentialHealth();
      return;
    }
    const result = await api.importJsonAccount(content);
    showToast(result.created ? t("accounts.importedNew") : result.restored ? t("accounts.importedRestored") : t("accounts.importedUpdated"));
    jsonDialog.open = false;
    await refreshAll();
    void loadCredentialHealth();
  } catch (error) {
    showToast((error as Error).message);
  } finally {
    jsonDialog.busy = false;
  }
}

/**
 * 切换：直接打开预检弹层并拉取只读预检，用户确认后才真正执行。
 * 预检本身就是「确认步骤」，所以不再走通用 ConfirmDialog（那会变成连点两次确认）。
 */
async function askSwitch(account: Account): Promise<void> {
  dialog.open = true;
  dialog.account = account;
  dialog.report = null;
  dialog.error = null;
  dialog.preview = null;
  dialog.confirming = true;
  // 程序位来自该平台最新的检测结果；默认选第一个**可写**的位。
  dialog.slots = clientDetections.value.find((item) => item.scope === account.scope)?.slots ?? [];
  dialog.slot = dialog.slots.find((item) => item.writable)?.key ?? "";
  dialog.busy = true;
  try {
    dialog.preview = await api.migrationPreview(account.id, account.scope);
  } catch (error) {
    dialog.error = (error as Error).message;
  } finally {
    dialog.busy = false;
  }
}

function askDelete(account: Account): void {
  confirm.mode = "delete";
  confirm.account = account;
  confirm.open = true;
}

async function onConfirm(): Promise<void> {
  const account = confirm.account;
  if (confirm.mode === "export") {
    confirm.open = false;
    await exportSelected();
    return;
  }
  if (!account && confirm.mode !== "batch-delete") return;
  confirm.busy = true;
  try {
    if (confirm.mode === "batch-delete") {
      const ids = [...selectedIds.value];
      let failed = 0;
      for (const id of ids) {
        try {
          await api.deleteAccount(id);
        } catch {
          failed += 1;
        }
      }
      clearSelection();
      showToast(failed ? t("accounts.deletePartial", { success: ids.length - failed, failed }) : t("accounts.deletedCount", { count: ids.length }));
      await refreshAll();
      // 刚删的号就在回收站里，角标要立刻反映出来，否则「去哪恢复」无从点起。
      void loadRecycleBin();
      confirm.open = false;
      return;
    }
    if (!account) return;
    await api.deleteAccount(account.id);
    showToast(t("accounts.deleted"));
    await refreshAll();
    void loadRecycleBin();
    confirm.open = false;
  } catch (error) {
    showToast((error as Error).message);
  } finally {
    confirm.busy = false;
  }
}

/** 预检确认后，真正执行切换。 */
async function onSwitchConfirm(): Promise<void> {
  const account = dialog.account;
  if (!account) return;
  dialog.confirming = false;
  dialog.busy = true;
  dialog.report = null;
  dialog.error = null;
  try {
    dialog.report = await api.switchAccount(account.id, undefined, true, dialog.slot || undefined);
    await refreshAll();
  } catch (error) {
    dialog.error = (error as Error).message;
  } finally {
    dialog.busy = false;
  }
}

/** 关闭切换弹层并复位状态。 */
function closeSwitchDialog(): void {
  dialog.open = false;
  dialog.confirming = false;
  dialog.preview = null;
  dialog.report = null;
  dialog.error = null;
  dialog.account = null;
  dialog.slots = [];
  dialog.slot = "";
}

const confirmText = computed(() => {
  const name = confirm.account?.displayName ?? "";
  if (confirm.mode === "export") {
    // 选中里混着缺 refresh token 的账号时，把「导出也解决不了重新登录」当场讲明。
    const base = t("accounts.export.message", { count: selectedCount.value });
    // 文案自带换行前缀，这里只拼内容。
    const gaps = selectedGapCount.value
      ? t("accounts.exportGap", { count: selectedGapCount.value })
      : "";
    return {
      title: t("accounts.export.title"),
      message: base + gaps,
      confirmText: t("accounts.export.confirm", { count: selectedCount.value }),
      tone: "danger" as const,
    };
  }
  if (confirm.mode === "batch-delete") {
    return {
      title: t("accounts.batchDelete.title"),
      // 选择里混着被筛掉的账号时挑明数量：删掉看不见的号是最容易挨骂的误操作。
      message: hiddenSelectedCount.value
        ? t("accounts.batchDelete.messageHidden", {
            count: selectedCount.value,
            hidden: hiddenSelectedCount.value,
          })
        : t("accounts.batchDelete.message", { count: selectedCount.value }),
      confirmText: t("accounts.batchDelete.confirm", { count: selectedCount.value }),
      tone: "danger" as const,
    };
  }
  return {
    title: t("accounts.delete.title"),
    message: t("accounts.delete.message", { name }),
    confirmText: t("accounts.delete.confirm"),
    tone: "danger" as const,
  };
});
</script>

<template>
  <div>
    <!-- 客户端扫描入池：已登录的客户端账号不在号池时提示 -->
    <div
      v-if="pendingPool"
      class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-[var(--radius-card)] border border-line bg-brand-soft px-4 py-3 text-[12.5px] text-brand-dark"
    >
      <UserPlus :size="15" :stroke-width="1.9" class="shrink-0" />
      <span class="flex-1">{{ t("accounts.pendingPoolHint") }}</span>
      <button
        type="button"
        class="inline-flex shrink-0 items-center gap-1.5 rounded-[9px] bg-brand px-3 py-1.5 text-[12px] font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-60"
        :disabled="importing"
        @click="onImport"
      >
        {{ importing ? t("accounts.importing") : t("accounts.poolIn") }}
      </button>
    </div>

    <!-- 凭据缺口：能切号但 token 一过期就要重登。早说清楚，别等用户撞上 -->
    <div
      v-if="gapAccounts.length"
      class="mb-3 flex items-start gap-2 rounded-[var(--radius-card)] border border-warn/30 bg-warn-soft px-4 py-2.5 text-[12.5px] leading-relaxed text-warn"
    >
      <AlertTriangle :size="15" :stroke-width="1.9" class="mt-0.5 shrink-0" />
      <div class="min-w-0 flex-1">
        <p>{{ t("accounts.credential.gapBanner", { count: gapAccounts.length }) }}</p>
        <p class="mt-0.5 text-[11.5px] opacity-85">
          {{ gapNames }} · {{ t("accounts.credential.gapBannerHint") }}
        </p>
      </div>
    </div>

    <div class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-[23.5px] font-semibold tracking-[-0.02em] text-ink">{{ t("accounts.title") }}</h1>
        <p class="mt-1.5 text-[13px] text-muted">{{ subtitle }}</p>
      </div>
    </div>

    <!-- 号池健康度：一眼看清可用/异常/到期/低积分/今日待做；能筛的整块可点
         数字是异步到的，用 AnimatedNumber 滚动 + 跳动，异常项点一颗呼吸圆点 -->
    <div class="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
      <component
        :is="stat.risk ? 'button' : 'div'"
        v-for="(stat, index) in health"
        :key="stat.key"
        :type="stat.risk ? 'button' : undefined"
        class="stat-tile min-w-0 rounded-[var(--radius-control)] border px-3 py-2 text-left transition-[border-color,box-shadow,background-color,transform] duration-200"
        :class="[
          stat.risk
            ? 'hover:-translate-y-px hover:border-[#d5d8d3] hover:shadow-card-hover'
            : 'cursor-default',
          stat.risk && riskFilter === stat.risk
            ? 'border-brand bg-brand-soft/60'
            : 'border-line bg-surface shadow-card',
        ]"
        :style="{ animationDelay: `${index * 45}ms` }"
        :title="
          stat.risk === 'total'
            ? t('accounts.health.clearHint')
            : stat.risk
              ? t('accounts.health.filterHint')
              : stat.hint
        "
        @click="applyRisk(stat.risk)"
      >
        <div class="flex items-center gap-1.5 text-[11px] text-muted">
          <span class="truncate">{{ stat.label }}</span>
          <span
            v-if="stat.tone === 'danger'"
            class="stat-dot stat-dot--danger shrink-0"
            aria-hidden="true"
          />
          <span v-else-if="stat.tone === 'warn'" class="stat-dot shrink-0" aria-hidden="true" />
        </div>
        <div class="mt-0.5 flex items-baseline gap-1">
          <AnimatedNumber
            :value="stat.value"
            :delay="index * 45"
            class="text-[18px] font-semibold leading-none"
            :class="TONE_TEXT[stat.tone]"
          />
          <span v-if="riskFilter === stat.risk" class="text-[10.5px] text-brand-dark">
            {{ t("accounts.health.filtered") }}
          </span>
        </div>
        <div class="mt-0.5 truncate text-[10.5px] text-muted">{{ stat.hint }}</div>
      </component>
    </div>

    <!-- 批量操作条：选中后出现在工具栏上方，跟着内容走，不浮在视口底部挡卡片 -->
    <div
      v-if="selectedCount"
      class="mt-5 flex flex-wrap items-center gap-2 rounded-[10px] border border-brand/30 bg-brand-soft px-3 py-2"
    >
      <span class="text-[12.5px] font-medium text-ink">
        {{ t("accounts.selectedOf", { count: selectedCount, total: visible.length }) }}
      </span>
      <span v-if="hiddenSelectedCount" class="text-[11.5px] text-warn">
        {{ t("accounts.hiddenSelected", { count: hiddenSelectedCount }) }}
      </span>
      <!-- 全选只在工具栏放一个；这里重复会让「选中共几个」和「再点一次全选」挤在一起 -->
      <button
        type="button"
        class="inline-flex items-center gap-1 rounded-[7px] px-2 py-1 text-[11.5px] text-brand-dark transition-colors hover:bg-surface/60"
        :title="t('accounts.invertHint')"
        @click="invertSelection"
      >
        <Shuffle :size="12" :stroke-width="2" />
        {{ t("accounts.invert") }}
      </button>
      <button
        v-if="hiddenSelectedCount"
        type="button"
        class="inline-flex items-center gap-1 rounded-[7px] px-2 py-1 text-[11.5px] text-brand-dark underline decoration-dotted transition-colors hover:bg-surface/60"
        :title="t('accounts.keepVisibleHint')"
        @click="keepVisibleOnly"
      >
        {{ t("accounts.keepVisible") }}
      </button>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[8px] border border-line bg-surface px-2.5 py-1.5 text-[12px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-60"
        :disabled="exporting"
        :title="t('accounts.exportHint')"
        @click="askExport"
      >
        <Loader2 v-if="exporting" :size="12" :stroke-width="2" class="animate-spin" />
        <Download v-else :size="12" :stroke-width="2" />
        {{ exporting ? t("accounts.exporting") : t("accounts.export") }}
      </button>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[8px] border border-danger/30 bg-surface px-2.5 py-1.5 text-[12px] text-danger transition-colors hover:bg-danger-soft"
        @click="askBatchDelete"
      >
        <Trash2 :size="12" :stroke-width="2" />
        {{ t("common.delete") }}
      </button>
      <button
        type="button"
        class="ml-auto inline-flex items-center gap-1 rounded-[8px] px-2 py-1.5 text-[12px] text-muted transition-colors hover:text-ink"
        :title="t('accounts.clearHint')"
        @click="clearSelection"
      >
        <X :size="12" :stroke-width="2" />
        {{ t("accounts.clearSelection") }}
      </button>
    </div>

    <div
      class="flex flex-wrap items-center gap-2"
      :class="selectedCount ? 'mt-2' : 'mt-5'"
    >
      <div class="relative w-[240px] shrink-0">
        <Search :size="14" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
        <input
          v-model="keyword"
          type="text"
          :placeholder="t('accounts.searchPlaceholder')"
          class="w-full rounded-[10px] border border-line bg-surface py-2 pl-9 pr-3 text-[13px] text-ink outline-none transition-colors placeholder:text-muted focus:border-brand"
        />
      </div>

      <!-- 平台筛选做成切换组：只有 2-3 个选项，下拉要多点一次 -->
      <div
        v-if="platformOptions.length > 2"
        class="flex shrink-0 items-center gap-1 rounded-[10px] border border-line bg-surface p-1"
      >
        <button
          v-for="option in platformOptions"
          :key="option.value"
          type="button"
          class="rounded-[7px] px-2.5 py-[5px] text-[12.5px] transition-colors"
          :class="
            platformFilter === option.value
              ? 'bg-line-soft text-ink'
              : 'text-muted hover:text-ink-soft'
          "
          @click="platformFilter = option.value"
        >
          {{ option.label }}
        </button>
      </div>

      <SelectMenu
        v-model="refreshSeconds"
        :options="refreshOptions"
        :title="t('accounts.refreshInterval')"
        class="w-[132px]"
      />

      <SelectMenu
        v-model="sortBy"
        :options="sortOptions"
        :title="t('accounts.sortBy')"
        class="w-[132px]"
      />

      <!-- 全选常驻：选中后不搬走，否则按钮会在眼前消失又从别处冒出来 -->
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[10px] border px-3 py-2 text-[13px] transition-colors disabled:opacity-60"
        :class="
          selectedCount
            ? 'border-brand/40 bg-brand-soft/60 text-brand-dark'
            : 'border-line bg-surface text-ink-soft hover:border-[#d5d8d3] hover:text-ink'
        "
        :disabled="!visible.length"
        :title="t('accounts.selectAllHint')"
        @click="toggleSelectAll"
      >
        <component
          :is="allSelected ? CheckSquare : someSelected ? MinusSquare : Square"
          :size="14"
          :stroke-width="1.9"
        />
        {{ allSelected ? t("accounts.unselectAll") : t("accounts.selectAll", { count: visible.length }) }}
      </button>

      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[10px] border border-line bg-surface px-3 py-2 text-[13px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-60"
        :disabled="!selectedCount || exporting"
        :title="selectedCount ? t('accounts.exportHint') : t('accounts.exportNeedSelection')"
        @click="askExport"
      >
        <Loader2 v-if="exporting" :size="14" :stroke-width="1.9" class="animate-spin" />
        <Download v-else :size="14" :stroke-width="1.9" />
        {{ exporting ? t("accounts.exporting") : t("accounts.export") }}
      </button>

      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[10px] border border-line bg-surface px-3 py-2 text-[13px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-60"
        :disabled="creditsLoading || loading"
        :title="t('accounts.reloadCredits')"
        @click="loadCredits(true)"
      >
        <Coins :size="14" :stroke-width="1.9" :class="creditsLoading ? 'animate-pulse' : ''" />
        {{ t("accounts.credits") }}
      </button>

      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[10px] border border-line bg-surface px-3.5 py-2 text-[13px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
        @click="oauthPlatform = 'workbuddy'; oauthOpen = true"
      >
        <QrCode :size="14" :stroke-width="1.9" />
        {{ t("accounts.scanWorkbuddy") }}
      </button>

      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[10px] border border-line bg-surface px-3.5 py-2 text-[13px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
        @click="oauthPlatform = 'trae'; oauthOpen = true"
      >
        <QrCode :size="14" :stroke-width="1.9" />
        {{ t("accounts.scanTrae") }}
      </button>

      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[10px] border border-line bg-surface px-3.5 py-2 text-[13px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-60"
        :disabled="jsonDialog.busy"
        @click="openJsonImport"
      >
        <ClipboardPaste :size="14" :stroke-width="1.9" />
        {{ t("accounts.jsonImport") }}
      </button>

      <!-- 删除确认框承诺「保留 30 天可恢复」，这里就是那个恢复入口 -->
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[10px] border border-line bg-surface px-3.5 py-2 text-[13px] transition-colors hover:border-[#d5d8d3] hover:text-ink"
        :class="recycle.items.length ? 'text-ink-soft' : 'text-muted hover:text-ink-soft'"
        :title="
          recycle.items.length
            ? t('accounts.recycle.countHint', { count: recycle.items.length })
            : t('accounts.recycle.openHint')
        "
        @click="openRecycle"
      >
        <History :size="14" :stroke-width="1.9" />
        {{ t("accounts.recycle.open") }}
        <span
          v-if="recycle.items.length"
          class="rounded-full bg-line-soft px-1.5 py-px text-[11px] text-ink-soft"
        >
          {{ recycle.items.length }}
        </span>
      </button>
    </div>

    <div v-if="riskFilter" class="mt-3 flex items-center gap-2 text-[12px] text-ink-soft">
      <span>{{ t("accounts.health.filterActive") }}</span>
      <button
        type="button"
        class="inline-flex items-center gap-1 rounded-full border border-brand/30 bg-brand-soft px-2.5 py-0.5 text-[11.5px] text-brand-dark"
        @click="riskFilter = ''"
      >
        {{ riskLabel }}
        <X :size="11" :stroke-width="2.4" />
      </button>
    </div>

    <div v-if="!visible.length" class="mt-6 rounded-[var(--radius-card)] border border-dashed border-line bg-surface px-6 py-14 text-center">
      <p class="text-[13px] text-ink-soft">
        {{ accounts.length ? t("accounts.noMatch") : t("accounts.empty") }}
      </p>
      <p class="mt-1.5 text-[12px] text-muted">
        {{ accounts.length ? t("accounts.noMatchHint") : t("accounts.emptyHint") }}
      </p>
    </div>

    <div v-else class="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
      <AccountCard
        v-for="account in shown"
        :key="account.id"
        :account="account"
        :is-current="isCurrent(account)"
        :switch-blocked-reason="switchBlockedReason"
        :busy="importing"
        :credit="credits[account.id] ?? null"
        :activity="activityBriefs[account.id] ?? null"
        :selected="selectedIds.has(account.id)"
        :preview="previews[account.id] ?? null"
        @switch="askSwitch"
        @remove="askDelete"
        @toggle="toggleSelect"
        @edit-note="openNote"
        @filter="applyRisk"
        @preview="loadPreview"
      />
    </div>

    <div v-if="hasMore" class="mt-4 flex justify-center">
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-[9px] border border-line bg-surface px-4 py-2 text-[12.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
        @click="loadMore"
      >
        {{ t("accounts.loadMore", { rest: visible.length - shown.length }) }}
      </button>
    </div>

    <NoteDialog
      :open="noteDialog.open"
      :account-name="noteDialog.account?.displayName ?? ''"
      :note="noteDialog.account?.note ?? ''"
      :busy="noteDialog.busy"
      @save="saveNote"
      @close="noteDialog.open = false"
    />

    <p v-if="switchBlockedReason" class="mt-4 text-[12px] text-muted">
      {{ t("accounts.hint", { reason: switchBlockedReason }) }}
    </p>

    <ConfirmDialog
      :open="confirm.open"
      :title="confirmText.title"
      :message="confirmText.message"
      :confirm-text="confirmText.confirmText"
      :tone="confirmText.tone"
      :busy="confirm.busy"
      @confirm="onConfirm"
      @cancel="confirm.open = false"
    />

    <SwitchDialog
      :open="dialog.open"
      :report="dialog.report"
      :busy="dialog.busy"
      :error="dialog.error"
      :preview="dialog.preview"
      :confirming="dialog.confirming"
      @close="closeSwitchDialog"
      @confirm="onSwitchConfirm"
    />

    <!-- 扫码登录：OAuth 采集 -->
    <OAuthLoginDialog
      :open="oauthOpen"
      :platform="oauthPlatform"
      @close="oauthOpen = false"
      @imported="refreshAll"
    />

    <!-- JSON 导入：上传/选择文件 或 粘贴登录态 JSON，平台由后端自动识别 -->
    <Teleport to="body">
      <div
        v-if="jsonDialog.open"
        class="fixed inset-0 z-[500] flex items-center justify-center bg-black/30 p-6"
        @click.self="jsonDialog.open = false"
      >
        <div class="w-full max-w-[560px] rounded-[14px] border border-line bg-surface p-5 shadow-[0_16px_48px_-12px_rgba(28,30,28,0.28)]">
          <div class="flex items-center justify-between">
            <h2 class="text-[15px] font-semibold text-ink">{{ t("accounts.jsonImport") }}</h2>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-[8px] border border-line bg-surface px-2.5 py-1.5 text-[12px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
              @click="pickFile"
            >
              <FileUp :size="13" :stroke-width="1.9" />
              {{ t("accounts.chooseFile") }}
            </button>
          </div>
          <p class="mt-1.5 text-[12px] leading-relaxed text-muted">
            {{ t("accounts.jsonDesc") }}
          </p>

          <input
            ref="fileInputRef"
            type="file"
            accept=".json,.txt,.info,application/json,text/plain"
            class="hidden"
            @change="onFileChosen"
          />

          <div
            class="mt-3 rounded-[10px] border border-dashed border-line bg-line-soft/40 px-4 py-3 transition-colors"
            :class="jsonDialog.fileName ? 'border-brand' : ''"
            @dragover="onDragOver"
            @drop.prevent="onFileDrop"
          >
            <div v-if="jsonDialog.fileName" class="flex items-center gap-2 text-[12.5px] text-ink">
              <FileUp :size="14" :stroke-width="1.9" class="text-brand" />
              <span class="min-w-0 flex-1 truncate">{{ jsonDialog.fileName }}</span>
              <span class="shrink-0 text-muted">{{ t("accounts.fileLoaded") }}</span>
            </div>
            <div v-else class="text-center text-[12px] text-muted">
              {{ t("accounts.dropHere") }}
            </div>
          </div>

          <textarea
            v-model="jsonDialog.content"
            rows="9"
            spellcheck="false"
            :placeholder="t('accounts.jsonPlaceholder')"
            class="mt-3 w-full resize-y rounded-[10px] border border-line bg-surface p-3 font-mono text-[12px] leading-relaxed text-ink outline-none transition-colors placeholder:text-muted focus:border-brand"
          ></textarea>

          <!-- 认出是本工具的导出包：明确告知含登录凭据，别让用户猜导入后有没有凭据 -->
          <div
            v-if="detectedBundle"
            class="mt-2 flex items-start gap-2 rounded-[9px] bg-brand-soft px-3 py-2 text-[12px] leading-relaxed text-brand-dark"
          >
            <Download :size="13" :stroke-width="1.9" class="mt-0.5 shrink-0" />
            <span>{{ t("accounts.bundleDetected", { count: detectedBundle.count }) }}</span>
          </div>

          <!-- 导出包里自带的缺口标记：导入前就说清「这几个过期要重登」 -->
          <div
            v-if="detectedBundle && detectedBundle.gapCount"
            class="mt-2 flex items-start gap-2 rounded-[9px] bg-warn-soft/70 px-3 py-2 text-[12px] leading-relaxed text-warn"
          >
            <AlertTriangle :size="13" :stroke-width="1.9" class="mt-0.5 shrink-0" />
            <span>{{ t("accounts.importBundleGap", { count: detectedBundle.gapCount }) }}</span>
          </div>

          <p v-if="jsonDialog.error" class="mt-2 text-[12px] text-danger">{{ jsonDialog.error }}</p>

          <div class="mt-4 flex justify-end gap-2">
            <button
              type="button"
              class="rounded-[9px] border border-line px-3.5 py-2 text-[12.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
              @click="jsonDialog.open = false"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="rounded-[9px] bg-brand px-3.5 py-2 text-[12.5px] font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-60"
              :disabled="jsonDialog.busy || !jsonDialog.content.trim()"
              @click="onJsonImport"
            >
              {{ jsonDialog.busy ? t("accounts.importing") : t("accounts.import") }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 回收站：软删除的账号在这里放 30 天，期间可恢复到号池 -->
    <Teleport to="body">
      <div
        v-if="recycle.open"
        class="fixed inset-0 z-[500] flex items-center justify-center bg-black/30 p-6"
        @click.self="recycle.open = false"
      >
        <div
          class="w-full max-w-[560px] rounded-[14px] border border-line bg-surface p-5 shadow-[0_16px_48px_-12px_rgba(28,30,28,0.28)]"
        >
          <div class="flex items-start justify-between gap-3">
            <div>
              <h2 class="text-[15px] font-semibold text-ink">{{ t("accounts.recycle.title") }}</h2>
              <p class="mt-1 text-[12px] leading-relaxed text-muted">
                {{ t("accounts.recycle.desc") }}
              </p>
            </div>
            <button
              type="button"
              class="shrink-0 rounded-[8px] p-1 text-muted transition-colors hover:bg-line-soft hover:text-ink"
              @click="recycle.open = false"
            >
              <X :size="15" :stroke-width="2" />
            </button>
          </div>

          <p v-if="recycle.error" class="mt-3 text-[12px] text-danger">
            {{ t("accounts.recycle.loadFailed", { reason: recycle.error }) }}
          </p>

          <div
            v-else-if="recycle.busy && !recycle.items.length"
            class="mt-4 flex items-center justify-center gap-2 py-8 text-[12.5px] text-muted"
          >
            <Loader2 :size="14" :stroke-width="2" class="animate-spin" />
            {{ t("accounts.recycle.busy") }}
          </div>

          <div
            v-else-if="!recycle.items.length"
            class="mt-4 rounded-[10px] border border-dashed border-line bg-line-soft/40 px-4 py-10 text-center"
          >
            <p class="text-[12.5px] text-ink-soft">{{ t("accounts.recycle.empty") }}</p>
            <p class="mt-1 text-[11.5px] text-muted">{{ t("accounts.recycle.emptyHint") }}</p>
          </div>

          <ul v-else class="mt-3 max-h-[336px] space-y-1.5 overflow-y-auto pr-1">
            <li
              v-for="item in recycle.items"
              :key="item.id"
              class="flex items-center gap-2.5 rounded-[10px] border border-line px-3 py-2"
            >
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-1.5">
                  <span class="truncate text-[12.5px] text-ink">{{ item.displayName }}</span>
                  <span
                    class="shrink-0 rounded-[5px] bg-line-soft px-1.5 py-px text-[10.5px] text-muted"
                  >
                    {{ platformLabel(item.platform) }}
                  </span>
                </div>
                <p class="mt-0.5 text-[11px] text-muted">
                  {{
                    item.daysLeft > 0
                      ? t("accounts.recycle.daysLeft", { days: item.daysLeft })
                      : t("accounts.recycle.dueToday")
                  }}
                </p>
              </div>
              <button
                type="button"
                class="inline-flex shrink-0 items-center gap-1 rounded-[8px] border border-brand/30 bg-brand-soft px-2.5 py-1.5 text-[12px] font-medium text-brand-dark transition-colors hover:bg-brand-soft/70 disabled:opacity-60"
                :disabled="recycle.busy"
                @click="restoreFromRecycle(item)"
              >
                <RotateCcw :size="12" :stroke-width="2" />
                {{ t("accounts.recycle.restore") }}
              </button>
            </li>
          </ul>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div
        v-if="toast"
        class="fixed bottom-8 left-1/2 z-[60] -translate-x-1/2 rounded-[9px] bg-ink px-4 py-2.5 text-[12.5px] text-white"
      >
        {{ toast }}
      </div>
    </Teleport>
  </div>
</template>
