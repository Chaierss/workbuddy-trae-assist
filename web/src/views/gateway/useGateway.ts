import { computed, inject, provide, reactive, type InjectionKey } from "vue";
import {
  api,
  type GatewayConfigPatch,
  type GatewayKey,
  type GatewayLogEntry,
  type GatewayPool,
  type GatewayPoolAccount,
  type GatewayProbeResult,
  type GatewayKeyUsage,
  type GatewayStatus,
} from "../../api";
import { t } from "../../lib/i18n";

/**
 * 网关模块的共享状态：容器建一份，6 个子 tab 各取所需。
 *
 * 用 `inject/provide` 而不是 props 逐层传：子 tab 数量会变，逐个接线太脆。
 * 状态装在 `reactive` 里，`Object.assign` 挂上 computed —— reactive 的 getter
 * 会自动拆包 ref，所以模板里直接 `store.activeKeys` 就是值，不用写 `.value`。
 */
export interface GatewayStore {
  status: GatewayStatus | null;
  keys: GatewayKey[];
  logs: GatewayLogEntry[];
  loading: boolean;
  saving: boolean;
  error: string | null;
  form: {
    host: string;
    port: number;
    allowNonLoopback: boolean;
    maxHops: number;
    catalogRefreshMinutes: number;
  };
  newLabel: string;
  /** 正在改名的 Key id（null = 没有在改名）与其草稿值。 */
  renaming: string | null;
  renameValue: string;
  /** 签发新 Key 的模型范围草稿：模式 + 能力 + 指定清单。 */
  newScope: string;
  newCapabilities: string[];
  newModels: string[];
  /** 正在编辑模型范围的 Key（null = 没开编辑框）。 */
  editing: {
    keyItem: GatewayKey;
    scope: string;
    capabilities: string[];
    models: string[];
  } | null;
  issued: string | null;
  busyKey: string | null;
  pending: { keyItem: GatewayKey; action: "revoke" | "restore" | "delete" } | null;
  copied: string;
  /** 账号池：每账号状态 + 人工排除 / 指定偏好。 */
  pool: GatewayPool | null;
  /** 探测用模型（默认取目录第一个，可改）。 */
  probeModel: string;
  /** 正在处置 / 探测的账号 id（按钮 loading 用）。 */
  busyAccount: string | null;
  /** 最近一次探测结果。 */
  probeResult: GatewayProbeResult | null;
  /** 请求日志筛选：空串 = 不筛。 */
  logKeyFilter: string;
  logSessionFilter: string;

  /** 单把 Key 的用量抽屉：null = 未打开。 */
  keyUsage: GatewayKeyUsage | null;
  keyUsageBusy: string | null;

  activeKeys: GatewayKey[];
  revokedKeys: GatewayKey[];
  dirty: boolean;

  load: () => Promise<void>;
  save: (patch: GatewayConfigPatch) => Promise<void>;
  issue: () => Promise<void>;
  beginRename: (key: GatewayKey) => void;
  cancelRename: () => void;
  saveKeyLabel: () => Promise<void>;
  beginEditScope: (key: GatewayKey) => void;
  saveKeyScope: () => Promise<void>;
  confirmKeyAction: () => Promise<void>;
  clearLogs: () => Promise<void>;
  refreshModels: () => Promise<void>;
  reloadLogs: () => Promise<void>;
  copy: (text: string, tag: string) => Promise<void>;
  updatePoolAccount: (
    account: GatewayPoolAccount,
    patch: { excluded?: boolean; preferred?: boolean },
  ) => Promise<void>;
  probeAccount: (account: GatewayPoolAccount) => Promise<void>;
  openKeyUsage: (key: GatewayKey) => Promise<void>;
  closeKeyUsage: () => void;
}

const GATEWAY_STORE: InjectionKey<GatewayStore> = Symbol("chaierww-gateway-store");

export function createGatewayStore(): GatewayStore {
  const state = reactive({
    status: null as GatewayStatus | null,
    keys: [] as GatewayKey[],
    logs: [] as GatewayLogEntry[],
    loading: false,
    saving: false,
    error: null as string | null,
    form: {
      host: "127.0.0.1",
      port: 8788,
      allowNonLoopback: false,
      maxHops: 3,
      catalogRefreshMinutes: 0,
    },
    newLabel: "",
    renaming: null as string | null,
    renameValue: "",
    newScope: "all",
    newCapabilities: [] as string[],
    newModels: [] as string[],
    editing: null as {
      keyItem: GatewayKey;
      scope: string;
      capabilities: string[];
      models: string[];
    } | null,
    issued: null as string | null,
    busyKey: null as string | null,
    pending: null as { keyItem: GatewayKey; action: "revoke" | "restore" | "delete" } | null,
    copied: "",
    pool: null as GatewayPool | null,
    probeModel: "",
    busyAccount: null as string | null,
    probeResult: null as GatewayProbeResult | null,
    logKeyFilter: "",
    logSessionFilter: "",
    keyUsage: null as GatewayKeyUsage | null,
    keyUsageBusy: null as string | null,
  });

  /** 日志筛选参数：空串不传，让后端不去做等值过滤。 */
  function logFilters(): { keyId?: string; sessionId?: string } {
    return {
      keyId: state.logKeyFilter || undefined,
      sessionId: state.logSessionFilter || undefined,
    };
  }

  function applyStatus(next: GatewayStatus): void {
    state.status = next;
    state.form = {
      host: next.host,
      port: next.port,
      allowNonLoopback: next.allowNonLoopback,
      maxHops: next.maxHops,
      catalogRefreshMinutes: next.catalogRefreshMinutes,
    };
    if (!state.probeModel && next.catalog.models.length) {
      state.probeModel = next.catalog.models[0].id;
    }
  }

  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      const [next, keyList, logList, pool] = await Promise.all([
        api.gatewayStatus(),
        api.gatewayKeys(),
        api.gatewayLogs(50, logFilters()),
        api.gatewayPool(),
      ]);
      applyStatus(next);
      state.keys = keyList.keys;
      state.logs = logList.logs;
      state.pool = pool;
      if (next.error) state.error = next.error;
      // 目录为空 = 从没抓过（或换了机器）：后台补一次，别让 Key 的模型选择器空着。
      if (!next.catalog.models.length) void ensureCatalog();
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.loading = false;
    }
  }

  /** 目录缺失时静默抓一次；失败不弹错（模型区域自己会显示「未获取」）。 */
  async function ensureCatalog(): Promise<void> {
    try {
      await api.refreshGatewayModels();
      applyStatus(await api.gatewayStatus());
    } catch {
      // 上游不可用时保持原状，用户可手动重试。
    }
  }

  /** 保存配置。端口占用这类失败由后端记在 status.error 里，这里如实转达。 */
  async function save(patch: GatewayConfigPatch): Promise<void> {
    state.saving = true;
    state.error = null;
    try {
      const next = await api.gatewayConfig(patch);
      applyStatus(next);
      if (next.error) state.error = next.error;
      state.logs = (await api.gatewayLogs(50, logFilters())).logs;
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.saving = false;
    }
  }

  async function issue(): Promise<void> {
    state.busyKey = "new";
    state.error = null;
    try {
      const result = await api.createGatewayKey({
        label: state.newLabel,
        scope: state.newScope,
        capabilities: state.newCapabilities,
        models: state.newModels,
      });
      state.issued = result.key;
      state.newLabel = "";
      state.newScope = "all";
      state.newCapabilities = [];
      state.newModels = [];
      state.keys = (await api.gatewayKeys()).keys;
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.busyKey = null;
    }
  }

  /** 打开改名：把当前备注填进草稿；双击备注也能触发。 */
  function beginRename(key: GatewayKey): void {
    state.renaming = key.id;
    state.renameValue = key.label;
  }

  function cancelRename(): void {
    state.renaming = null;
    state.renameValue = "";
  }

  /** 只改备注：模型范围、明文与用量都不动。 */
  async function saveKeyLabel(): Promise<void> {
    const keyId = state.renaming;
    if (!keyId) return;
    const label = state.renameValue.trim();
    if (!label) {
      state.error = t("gateway.keys.labelRequired");
      return;
    }
    state.busyKey = keyId;
    state.error = null;
    try {
      await api.updateGatewayKey(keyId, { label });
      cancelRename();
      state.keys = (await api.gatewayKeys()).keys;
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.busyKey = null;
    }
  }

  /** 打开「编辑模型范围」：把该 Key 当前的范围拷进草稿，改完再保存。 */
  function beginEditScope(key: GatewayKey): void {
    state.editing = {
      keyItem: key,
      scope: key.scope || "all",
      capabilities: [...key.capabilities],
      models: [...key.models],
    };
  }

  /** 保存模型范围：只 PUT 这三个字段，Key 本身与明文都不动。 */
  async function saveKeyScope(): Promise<void> {
    const draft = state.editing;
    if (!draft) return;
    state.busyKey = draft.keyItem.id;
    state.error = null;
    try {
      await api.updateGatewayKey(draft.keyItem.id, {
        scope: draft.scope,
        capabilities: draft.capabilities,
        models: draft.models,
      });
      state.editing = null;
      state.keys = (await api.gatewayKeys()).keys;
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.busyKey = null;
    }
  }

  async function confirmKeyAction(): Promise<void> {
    const item = state.pending;
    if (!item) return;
    state.pending = null;
    state.busyKey = item.keyItem.id;
    state.error = null;
    try {
      if (item.action === "revoke") {
        await api.revokeGatewayKey(item.keyItem.id);
      } else if (item.action === "restore") {
        await api.restoreGatewayKey(item.keyItem.id);
      } else {
        await api.deleteGatewayKey(item.keyItem.id);
      }
      state.keys = (await api.gatewayKeys()).keys;
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.busyKey = null;
    }
  }

  async function clearLogs(): Promise<void> {
    state.error = null;
    try {
      await api.clearGatewayLogs();
      state.logs = [];
      if (state.status) state.status = await api.gatewayStatus();
    } catch (cause) {
      state.error = (cause as Error).message;
    }
  }

  /** 筛选条件变了就重拉日志（只重拉日志，不动其它模块的状态）。 */
  async function reloadLogs(): Promise<void> {
    state.error = null;
    try {
      state.logs = (await api.gatewayLogs(50, logFilters())).logs;
    } catch (cause) {
      state.error = (cause as Error).message;
    }
  }

  async function refreshModels(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      await api.refreshGatewayModels();
      applyStatus(await api.gatewayStatus());
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.loading = false;
    }
  }

  /** 排除 / 恢复、设为 / 取消指定偏好：后端回最新池子快照，直接覆盖。 */
  async function updatePoolAccount(
    account: GatewayPoolAccount,
    patch: { excluded?: boolean; preferred?: boolean },
  ): Promise<void> {
    state.busyAccount = account.id;
    state.error = null;
    try {
      state.pool = await api.updateGatewayPoolAccount(account.id, patch);
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.busyAccount = null;
    }
  }

  /** 探测某账号：成功即解冷却，所以探测完顺手刷新池子与日志。 */
  async function probeAccount(account: GatewayPoolAccount): Promise<void> {
    state.busyAccount = account.id;
    state.error = null;
    try {
      state.probeResult = await api.probeGatewayAccount(account.id, state.probeModel);
      state.pool = await api.gatewayPool();
      state.logs = (await api.gatewayLogs(50, logFilters())).logs;
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.busyAccount = null;
    }
  }

  async function openKeyUsage(key: GatewayKey): Promise<void> {
    state.keyUsageBusy = key.id;
    state.error = null;
    try {
      state.keyUsage = await api.gatewayKeyUsage(key.id);
    } catch (cause) {
      state.error = (cause as Error).message;
    } finally {
      state.keyUsageBusy = null;
    }
  }

  function closeKeyUsage(): void {
    state.keyUsage = null;
  }

  async function copy(text: string, tag: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      state.copied = tag;
      window.setTimeout(() => {
        if (state.copied === tag) state.copied = "";
      }, 1600);
    } catch {
      state.copied = "";
    }
  }

  return Object.assign(state, {
    activeKeys: computed(() => state.keys.filter((key) => key.active)),
    revokedKeys: computed(() => state.keys.filter((key) => !key.active)),
    dirty: computed(() => {
      const current = state.status;
      if (!current) return false;
      return (
        state.form.host !== current.host ||
        state.form.port !== current.port ||
        state.form.allowNonLoopback !== current.allowNonLoopback ||
        state.form.maxHops !== current.maxHops ||
        state.form.catalogRefreshMinutes !== current.catalogRefreshMinutes
      );
    }),
    load,
    save,
    issue,
    beginRename,
    cancelRename,
    saveKeyLabel,
    beginEditScope,
    saveKeyScope,
    confirmKeyAction,
    clearLogs,
    refreshModels,
    reloadLogs,
    copy,
    updatePoolAccount,
    probeAccount,
    openKeyUsage,
    closeKeyUsage,
  }) as unknown as GatewayStore;
}

export function provideGateway(store: GatewayStore): void {
  provide(GATEWAY_STORE, store);
}

export function useGateway(): GatewayStore {
  const store = inject(GATEWAY_STORE);
  if (!store) throw new Error(t("gateway.storeNotProvided"));
  return store;
}

/** 把大数字缩成 `1.2K` / `3.4M`，看板与统计卡共用。 */
export function compact(value: number): string {
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}K`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}
