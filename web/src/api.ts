import { t } from "./lib/i18n";

export interface ScopeInfo {
  key: string;
  platform: string;
  region: string;
  label: string;
  implemented: boolean;
}

export interface Account {
  id: string;
  scope: string;
  platform: string;
  externalId: string;
  displayName: string;
  note: string;
  avatarPath: string | null;
  source: string;
  credentialState: "usable" | "unverified" | "relogin_required" | "unknown";
  lastUsedAt: string | null;
  createdAt: string;
  updatedAt: string;
  deletedAt: string | null;
}

/** 单个账号的导出物：凭据是解密后的明文，等价于登录态本身。 */
export interface AccountBundleEntry {
  scope: string;
  externalId: string;
  displayName: string;
  note: string;
  credentialKind: string;
  credential: Record<string, unknown>;
}

/** `/api/accounts/export` 的产物，可直接下载成 .json 再导回来。 */
export interface AccountBundle {
  format: "chaierww.accounts";
  version: number;
  exportedAt: string;
  accounts: AccountBundleEntry[];
  skipped: number;
}

export interface AccountBundleImportResult {
  total: number;
  imported: number;
  created: number;
  restored: number;
  failed: number;
  errors: string[];
}

export interface CreditPackage {
  name: string;
  remaining: number;
  total: number | null;
  expiresAt: number | null;
  kind: string;
}

export interface CreditInfo {
  /** false = 该凭据形态查不了（不是 0 积分）。 */
  available: boolean;
  credits: number | null;
  /** 总额度（官方字段）。查不到为 null。 */
  total: number | null;
  /** 最早到期时间（unix 秒）。 */
  expiresAt: number | null;
  expiringSoon: boolean;
  error: string | null;
  updatedAt: string | null;
  /** 订阅 / 套餐档位，如「体验版」「会员 Pro 连续包月」。 */
  subscription: string | null;
  isPaid: boolean | null;
  /** 积分包明细（套餐基础 / 赠送 / 购买 / 会员等）。 */
  packages: CreditPackage[];
}

export type ActivityGroup = "daily" | "seasonal";

/** 活动状态。与「能不能自动做」（automatable）是两个维度。 */
export type ActivityState = "done" | "todo" | "in_progress" | "not_started" | "unavailable";

export interface ActivityItem {
  key: string;
  platform: string;
  group: ActivityGroup;
  title: string;
  subtitle: string;
  state: ActivityState;
  automatable: boolean;
  /** 不可自动化时的原因，必须展示给用户。 */
  manualReason: string;
  progressCurrent: number | null;
  progressTarget: number | null;
  progressText: string;
  reward: number | null;
  /** 已格式化好的收益文案；空串表示上游没给数字（不编）。 */
  rewardText: string;
  periodStart: string | null;
  periodEnd: string | null;
  note: string;
  /** 手动项的客户端深链：工具做不了，但可以一键把用户送到页面。 */
  jumpUrl?: string;
  /** 仍需手动完成的条目（标题 / 说明 / 深链），逐条列出。 */
  manualTasks?: Array<{ code: string; title: string; desc: string; jumpUrl: string }>;
}

export interface ActivityCatalogEntry {
  key: string;
  platform: string;
  group: ActivityGroup;
  title: string;
  subtitle: string;
  description: string;
  rewardHint: string;
  automatable: boolean;
}

export interface AccountActivities {
  accountId: string;
  displayName: string;
  platform: string;
  scope: string;
  /** false = 这个账号的凭据形态查不了（不是「没有活动」）。 */
  detectable: boolean;
  error: string | null;
  updatedAt: string | null;
  activities: ActivityItem[];
}

/** 号池卡片上的活动余量摘要：与活动页「一键执行」同一口径。 */
export interface ActivityBrief {
  total: number;
  done: number;
  /** 能自动做且还没做的项数。 */
  automated: number;
  /** 仍需手动做的项数（不含查不到的）。 */
  manual: number;
  /** 每项活动的状态，按顺序画成小段：done / auto / manual / unknown。 */
  states: Array<"done" | "auto" | "manual" | "unknown">;
}

export interface ActivitySummary {
  accounts: number;
  detectable: number;
  /** 能自动做且还没做的项数 —— 与「一键执行」直接对应。 */
  actionable: number;
  todo: number;
  inProgress: number;
  done: number;
  manual: number;
  unavailable: number;
}

export interface ActivityRunResult {
  accountId: string;
  displayName: string;
  platform: string;
  key: string;
  title: string;
  status: "ok" | "skipped" | "failed";
  message: string;
  reward: number | null;
}

export interface ActivityRunReport {
  results: ActivityRunResult[];
  summary: { total: number; ok: number; skipped: number; failed: number };
}

export interface RequestRow {
  requestId: string;
  credit: number;
  model: string;
  client: string;
  requestTime: string;
  date: string;
  /** 本机能关联到的会话 id；null = 关联不到（点开只能提示「本机无对话记录」）。 */
  conversationId: string | null;
}

export interface ModelUsage {
  model: string;
  requests: number;
  credit: number;
}

export interface DailyPoint {
  date: string;
  requests: number;
  credit: number;
}

/** 一次会话（客户端 cid）在统计窗口里花掉的积分与请求数。 */
export interface ConversationUsage {
  conversationId: string;
  title: string;
  cwd: string;
  requests: number;
  credit: number;
  lastTime: string;
}

export interface AccountUsage {
  accountId: string;
  displayName: string;
  platform: string;
  scope: string;
  /** false = 查不了（不是没用过）。 */
  detectable: boolean;
  requests: number;
  creditConsumed: number;
  todayRequests: number;
  todayCredit: number;
  weekRequests: number;
  weekCredit: number;
  monthRequests: number;
  monthCredit: number;
  models: ModelUsage[];
  daily: DailyPoint[];
  detail: RequestRow[];
  detailTruncated: boolean;
  /** 按会话聚合的消耗（关联不到本机会话时为空）。 */
  conversations: ConversationUsage[];
  /** 明细里能关联到本机会话的行数（关联率说明用）。 */
  linkedRequests: number;
  creditsConsumed: number | null;
  creditsTotal: number | null;
  error: string | null;
  updatedAt: string | null;
}

export interface UsageSummary {
  accounts: number;
  detectable: number;
  totalRequests: number;
  totalCreditConsumed: number;
  workbuddyCreditConsumed: number;
  traeCreditsConsumed: number | null;
}

/** 一条对话正文（user / assistant 气泡）。 */
export interface ConversationMessage {
  role: "user" | "assistant";
  text: string;
  /** 毫秒时间戳。 */
  at: number | null;
  model: string | null;
}

/** 一次计费请求还原出的对话（明细行点开抽屉用）。 */
export interface UsageRequestTurn {
  accountId: string;
  requestId: string;
  found: boolean;
  conversationId: string | null;
  title: string;
  cwd: string;
  messages: ConversationMessage[];
  truncated: boolean;
  error: string | null;
}

/** 一条计费请求的对话摘要（明细表直接用，不再逐行点开）。 */
export interface UsageTurnBrief {
  requestId: string;
  conversationId: string | null;
  available: boolean;
  messageCount: number;
  /** 首条提问（截断）。 */
  userText: string;
  /** 末条回答（截断）。 */
  assistantText: string;
  model: string | null;
  title: string;
  error: string | null;
}

/** 一个「程序位」：某个客户端宿主（桌面端 / IDE / CLI / 编辑器插件）。 */
export interface ClientSlot {
  key: string;
  label: string;
  /** false = 只读 / 未适配：界面上灰掉并展示 `reason`，绝不写。 */
  writable: boolean;
  installed: boolean;
  stateFile: string | null;
  appPath: string | null;
  /** 不可写的原因；空串表示该位可写。 */
  reason: string;
}

export interface Detection {
  scope: string;  installed: boolean;
  running: boolean;
  loggedIn: boolean;
  stateDir: string | null;
  appPath: string | null;
  message: string;
  currentAccountId: string | null;
  slots: ClientSlot[];
}

export interface ClientDetection {
  scope: string;
  platform: string;
  label: string;
  installed: boolean;
  running: boolean;
  loggedIn: boolean;
  stateDir: string | null;
  appPath: string | null;
  message: string;
  currentAccountId: string | null;
  downloadUrl: string | null;
  slots: ClientSlot[];
}

export interface Snapshot {
  id: string;
  scope: string;
  createdAt: string;
  reason: string;
  accountId: string | null;
  totalBytes: number;
  fileCount: number;
  verified: boolean;
  note: string;
}

/** 全局备份的一个分区。`credential` = 含凭据，勾选它必须二次确认。 */
export interface GlobalBackupPart {
  key: string;
  label: string;
  credential: boolean;
  /** 该分区收录的文件名，界面上作为副文本展示。 */
  files: string[];
}

/** 全局备份概览：分区清单（由后端给出）+ 已有备份点。 */
export interface GlobalBackupOverview {
  scope: string;
  parts: GlobalBackupPart[];
  /** 默认勾选的分区，按设计不含凭据。 */
  defaultParts: string[];
  /** 自动备份当前收录的分区（用户勾选含凭据分区确认后随之更新）。 */
  autoParts: string[];
  snapshots: Snapshot[];
}

export interface StepOutcome {
  step: string;
  ok: boolean;
  detail: string;
}

export interface PlannedFile {
  path: string;
  action: string;
  size: number;
  note: string;
}

export interface SwitchReport {
  state: string;
  dryRun: boolean;
  succeeded: boolean;
  scope: string;
  accountId: string;
  snapshotId: string | null;
  error: string | null;
  rollbackFailed: boolean;
  steps: StepOutcome[];
  plan: {
    requiresAppExit: boolean;
    warnings: string[];
    files: PlannedFile[];
  } | null;
  /** 切换成功后的数据迁移报告（记忆/连接器/会话），仅 WorkBuddy 有。 */
  migration: MigrationReport | null;
}

export interface MigrationReport {
  sourceUid: string;
  targetUid: string;
  changed: boolean;
  memory: {
    targetLines?: number;
    sourceLines?: number;
    appended?: number;
    skippedDuplicate?: number;
    changed?: boolean;
    backup?: string | null;
    error?: string;
  } | null;
  connectors: {
    skipped?: boolean;
    addedKeys?: number;
    droppedDuplicateElements?: number;
    changed?: boolean;
    backup?: string | null;
    error?: string;
  } | null;
  sessions: {
    copiedCount?: number;
    skippedCount?: number;
    errorCount?: number;
    backup?: string | null;
    backupError?: string | null;
    error?: string;
  } | null;
  /** 个人档案与偏好（user-{uid}-personal/ 等）合并报告。 */
  profile?: MergeBlockReport | null;
  /** 工作区与项目元数据合并报告。 */
  workspaces?: MergeBlockReport | null;
}

export interface MergeBlockReport {
  mergedFiles?: string[];
  mergedCount?: number;
  skipped?: string[];
  errors?: { where: string; error: string }[];
  changed?: boolean;
  backup?: string | null;
  error?: string;
}

/** 迁移前预检结果（只读推演，与实际执行同一套判定）。 */
export interface MigrationPreview {
  sourceUid: string;
  targetUid: string;
  valid: boolean;
  reason?: string;
  memory: {
    targetLines: number;
    sourceLines: number;
    wouldAppend: number;
    wouldSkip: number;
    changed: boolean;
  } | null;
  connectors: {
    skipped: boolean;
    files: { name: string; addedKeys?: number; error?: string }[];
    addedKeys: number;
    droppedDupe: number;
  } | null;
  sessions: {
    wouldCopy: { id: string; title: string; rootId?: string }[];
    wouldSkip: { id: string; reason: string; rootId?: string }[];
    copyCount: number;
    skipCount: number;
    error?: string;
  } | null;
  profile: {
    willMerge: string[];
    willSkip: string[];
    skipped: string[];
    mergeCount: number;
    error?: string;
  } | null;
  workspaces: {
    willMerge: string[];
    willSkip: string[];
    skipped: string[];
    mergeCount: number;
    error?: string;
  } | null;
  /** 目标账号里已存在的同血统重复会话（建议清理）。 */
  duplicates: {
    rootUid: string;
    rootId: string;
    kept: { id: string; title: string };
    dups: { id: string; title: string }[];
  }[];
}

export interface HealthInfo {
  status: string;
  secrets: string;
  dataDir: string;
}

export interface Preferences {
  /** 切换时把源账号的记忆合并到目标账号。 */
  migrateMemory: boolean;
  /** 切换时把源账号的连接器配置合并到目标账号。 */
  migrateConnectors: boolean;
  /** 切换时把源账号的会话复制到目标账号。 */
  migrateSessions: boolean;
  /** 切换时把源账号的个人档案与偏好合并到目标账号。 */
  migrateProfile: boolean;
  /** 切换时把源账号的工作区与项目元数据并集到目标账号。 */
  migrateWorkspaces: boolean;
  /** 每日自动备份，默认开启；每个客户端只留最近 7 份快照。界面已无开关。 */
  autoBackup: boolean;
  /** 每日自动做一份全局备份（默认分区，不含凭据），默认开启。开关在设置 → 备份页。 */
  autoGlobalBackup: boolean;
  /** 自动备份收录的分区；勾选含凭据分区并确认后会随之更新。 */
  autoGlobalParts: string[];
}

/** 一个计划任务：保活 / 签到 / 全部活动。 */
export interface ScheduleSpec {
  key: string;
  enabled: boolean;
  /** 一天里的执行时点，`"HH:MM"` 可多个。 */
  times: string[];
  /** 随机抖动半径（分钟）：0 / 5 / 15 / 30，实际偏移在同一天内稳定。 */
  jitterMinutes: number;
  /** 限定参与的账号；空数组 = 号池里全部账号。 */
  accountIds: string[];
  lastRunAt: string | null;
  lastResult: string;
  /** 最近若干次执行记录（时间 + 结果摘要），旧的在前，只保留最近 7 条。 */
  history: { at: string; result: string }[];
  nextRunAt: string | null;
}

/** 自动轮换策略（防积分过期）。默认关闭，须在设置里手动开。 */
export interface RotationPolicy {
  enabled: boolean;
  /** 参与轮换的作用域；界面只读展示，默认 WorkBuddy 国内版。 */
  scope: string;
  /** 最紧迫账号的剩余有效期超过这个小时数就不切。 */
  minUrgencyHours: number;
  /** 距上次轮换不足这个分钟数就不切。 */
  cooldownMinutes: number;
  /** 当前账号在这个分钟数内被用过就不切（别把默认账号从手边抽走）。 */
  activeGuardMinutes: number;
  /** 目标账号剩余积分低于这个值就不值得切。 */
  minRemainingCredits: number;
  /** 目标只比当前账号早这么多小时以内到期就不切（防抖动）。 */
  minGapHours: number;
}

/** 台账的一行：某轮「切 / 不切」的结论，以及到底切了没有。 */
export interface RotationLedgerEntry {
  at: string;
  action: "switch" | "none";
  /** 判定结论来自哪一条规则：valid_accounts / urgency / … / ok。 */
  rule: string;
  reason: string;
  targetAccountId: string | null;
  targetName: string;
  executed: boolean;
  /** 执行侧的说明：成功、被跳过（客户端运行中）、只推演等。 */
  detail: string;
}

export interface RotationState {
  policy: RotationPolicy;
  lastEvaluatedAt: string | null;
  lastSwitchAt: string | null;
  history: RotationLedgerEntry[];
}

export interface RotationDecision {
  action: "switch" | "none";
  rule: string;
  reason: string;
  evaluatedAt: string;
  currentAccountId: string | null;
  targetAccountId: string | null;
  targetName: string;
  hoursToExpiry: number | null;
  remaining: number | null;
}

/** 客户端设置里一个**已登记**的键。未登记的键后端既不读也不返回。 */
export interface ClientSettingSpec {
  key: string;
  label: string;
  kind: "string" | "bool";
  /** 键不存在时界面显示的回退值。 */
  default: string | boolean;
  hint: string;
  /** false = 客户端文件里还没有这个键（保存时会按最小插入补上）。 */
  present: boolean;
  /** 当前值；`null` 只在读取失败时出现。 */
  value: string | boolean | null;
}

export interface ClientSettingsBackup {
  id: string;
  size: number;
  createdAt: string;
}

/** 客户端设置快照：登记键 + 格式指纹 + 备份点。 */
export interface ClientSettingsSnapshot {
  /** false = 读不到客户端设置文件（未安装 / 文件不存在）。 */
  available: boolean;
  path: string;
  /** 写入时要用它做比对；`null` = 文件读不到。 */
  fingerprint: string | null;
  message: string;
  specs: ClientSettingSpec[];
  backups: ClientSettingsBackup[];
}

export interface ClientSettingsWriteResult {
  changed: { key: string; label: string; before: unknown; after: unknown }[];
  fingerprint: string | null;
  /** 写入前自动落的备份点；无变更时为 null。 */
  backupId: string | null;
  settings: ClientSettingsSnapshot;
}

/** 「待处理」清单里的一项。`action` 描述按钮该做什么，前端不写死跳转。 */
export interface ActionableItem {
  /** 服务端取值：credential | checkin | credits_expiring | client_offline；页面本地也补几项引导态。 */
  kind: string;
  severity: "high" | "warn" | "info";
  title: string;
  detail: string;
  accountId: string | null;
  action:
    | { type: "navigate"; to: string }
    | { type: "run"; endpoint: string; accountIds: string[] };
}

/** 概览「今日速览」：全部取自已缓存的检测结果 + 网关本地计数，不额外打上游。 */
export interface OverviewPulse {
  accounts: number;
  detectable: number;
  activityActionable: number;
  activityDone: number;
  activityTotal: number;
  credits: number;
  creditsExpiring: number;
  clientsRunning: number;
  gateway: {
    requests: number;
    errors: number;
    tokens: number;
    activeKeys: number;
  };
}

/** 网关对外暴露的模型（`id` 就是上游原名，转发时原样透传）。 */
export interface GatewayCatalogModel {
  id: string;
  name: string;
  ownedBy: string;
  contextWindow: number;
  supportsImages: boolean;
  free: boolean;
  badges: string[];
  /** 单次回复的最大输出 token（模型能力标注）。 */
  maxOutputTokens: number;
  /** 上游计费串，如 `x0.00` / `x0.5`（空串 = 无标注）。 */
  credits: string;
}

/** 一把网关 Key。明文只在签发那一刻返回，之后只有掩码。 */
export interface GatewayKey {
  id: string;
  label: string;
  mask: string;
  createdAt: string;
  lastUsedAt: string | null;
  revokedAt: string | null;
  requests: number;
  tokens: number;
  active: boolean;
  /** 静态白名单（`scope="list"` 时生效）。 */
  models: string[];
  /** 模型范围：`all`（全部，目录新增自动可用）/ `capability`（按能力）/ `list`（指定）。 */
  scope: string;
  /** `scope="capability"` 时选中的能力键：`free` / `image` / `long_context`。 */
  capabilities: string[];
}

export interface GatewayKeyModelUsage {
  model: string;
  requests: number;
  tokens: number;
  errors: number;
}

/** 单把 Key 的用量：`sample` 只覆盖当前保留的日志，不能当全量看。 */
export interface GatewayKeyUsage {
  key: GatewayKey;
  sample: {
    scope: number;
    requests: number;
    ok: number;
    failed: number;
    tokens: number;
    avgDurationMs: number | null;
  };
  byModel: GatewayKeyModelUsage[];
  series: {
    bucket: string;
    requests: number;
    tokens: number;
    errors: number;
  }[];
  recent: GatewayLogEntry[];
}

export interface GatewayLogEntry {
  id: string;
  at: string;
  /** false = 这次请求最终失败（换号全试完或上游 4xx）。 */
  ok: boolean;
  stream: boolean;
  status: number;
  model: string;
  requestedModel: string;
  keyId: string;
  keyLabel: string;
  accountId: string | null;
  accountName: string | null;
  durationMs: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  /** 换过几次号（跳过的不算成功）。 */
  hops: number;
  skipped: string[];
  error: string;
  /** 客户端传入的会话标识；取不到时是空串（界面显示为 —）。 */
  sessionId: string;
  /** 请求内容摘要：最后一条用户消息的文本（截断）；取不到时是空串。 */
  promptPreview: string;
}

export interface GatewayStats {
  requests: number;
  errors: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  lastRequestAt: string | null;
  breakdown: GatewayBreakdown;
}

/** 网关用量看板：从最近保留的日志（最多 200 条）派生，非全量累计。 */
export interface GatewayBreakdown {
  /** 采样口径 —— 与日志保留上限一致。 */
  scope: number;
  sampledAt: string;
  total: number;
  byModel: { model: string; requests: number; tokens: number; errors: number }[];
  byAccount: {
    accountId: string;
    accountName: string;
    requests: number;
    tokens: number;
    ok: number;
    failed: number;
    hops: number;
  }[];
  /** 每把下发的 Key 在这批采样里用了多少（与 Key 记录上的累计值不是同一口径）。 */
  byKey: { keyId: string; keyLabel: string; requests: number; tokens: number; ok: number; failed: number }[];
  series: { bucket: string; requests: number; tokens: number; errors: number }[];
  successRate: number | null;
  avgDurationMs: number | null;
  totalHops: number;
}

export interface GatewayStatus {
  enabled: boolean;
  host: string;
  port: number;
  allowNonLoopback: boolean;
  maxHops: number;
  /** 模型目录自动刷新间隔（分钟）；0 = 关闭。 */
  catalogRefreshMinutes: number;
  baseUrl: string;
  chatUrl: string;
  modelsUrl: string;
  /** true = 只绑回环地址，外部网络打不进来。 */
  loopbackOnly: boolean;
  /** false = 端口没在监听（开关没开，或端口被占用）。 */
  running: boolean;
  error: string | null;
  activeKeys: number;
  /** 号池里可供转发的账号数。 */
  accounts: number;
  stats: GatewayStats;
  /** `fresh` = 缓存仍在 10 分钟 TTL 内；`fetchedAt` = 上次抓取的墙上时间（没抓过为 null）。 */
  catalog: { models: GatewayCatalogModel[]; fresh: boolean; fetchedAt: string | null };
}

export interface GatewayConfigPatch {
  enabled?: boolean;
  host?: string;
  port?: number;
  allowNonLoopback?: boolean;
  maxHops?: number;
  catalogRefreshMinutes?: number;
}

export interface GatewayPoolAccount {
  id: string;
  name: string;
  credentialState: "usable" | "unverified" | "relogin_required" | "unknown";
  /** usable = 可用；cooling = 冷却中；exhausted = 积分耗尽；excluded = 已排除。 */
  state: "usable" | "cooling" | "exhausted" | "excluded" | "unavailable";
  /** 冷却剩余秒数（0 = 可用）。 */
  cooldownSeconds: number;
  excluded: boolean;
  /** true = 网关当前指定的偏好账号（它可用时优先被挑中）。 */
  preferred: boolean;
  /** null = 还没查到余额（或这种凭据查不了），不是「0 积分」。 */
  credits: CreditInfo | null;
}

export interface GatewayPool {
  scope: string;
  preferred: string | null;
  accounts: GatewayPoolAccount[];
}

export interface GatewayProbeResult {
  ok: boolean;
  /** 上游状态码；0 = 请求没发出去（凭据构造失败 / 不可达）。 */
  status: number;
  model: string;
  accountId: string;
  accountName: string;
  error: string;
}

export interface DocEntry {
  path: string;
  title: string;
  group: string;
  size: number;
  updatedAt: string;
}

export interface DocContent {
  path: string;
  title: string;
  content: string;
}

export interface DocsStatus {
  available: boolean;
  docsUrl: string;
  openapiUrl: string;
  reason: string;
}

export interface LogFile {
  name: string;
  size: number;
  updatedAt: string;
}

export interface LogEntry {
  file: string;
  at: string | null;
  level: string;
  logger: string;
  message: string;
}

export interface LogPage {
  page: number;
  pageSize: number;
  hasMore: boolean;
  level: string | null;
  keyword: string;
  file: string;
  entries: LogEntry[];
}

export interface LogQuery {
  page?: number;
  pageSize?: number;
  level?: string;
  keyword?: string;
  file?: string;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { detail: text };
  }
  if (!response.ok) {
    const detail = (payload as { detail?: string } | null)?.detail;
    throw new Error(detail || t("api.requestFailed", { status: response.status }));
  }
  return payload as T;
}

export const api = {
  health: () => request<HealthInfo>("/api/health"),

  scopes: () => request<{ scopes: ScopeInfo[] }>("/api/scopes"),

  detect: (scope: string) =>
    request<Detection>(`/api/detect?scope=${encodeURIComponent(scope)}`),

  /** 合并检测：一次返回所有已实现客户端的安装/运行/登录状态 + 引导下载链接。 */
  detectAll: () => request<{ clients: ClientDetection[] }>("/api/detection"),

  preferences: () => request<Preferences>("/api/preferences"),

  savePreferences: (patch: Partial<Preferences>) =>
    request<Preferences>("/api/preferences", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  /** 计划任务列表（保活 / 签到 / 全部活动），含下次执行时间与上次结果。 */
  schedules: () => request<{ tasks: ScheduleSpec[] }>("/api/schedules"),

  /** 改计划任务：时点 / 抖动 / 启用 / 账号范围，省略的字段保持原值。 */
  updateSchedule: (
    key: string,
    patch: Partial<Pick<ScheduleSpec, "enabled" | "times" | "jitterMinutes" | "accountIds">>,
  ) =>
    request<{ task: ScheduleSpec }>(`/api/schedules/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  /** 立即执行一次计划任务（不等时点）。 */
  runScheduleNow: (key: string) =>
    request<{ key: string; result: string; lastRunAt: string | null; task: ScheduleSpec | null }>(
      `/api/schedules/${encodeURIComponent(key)}/run-now`,
      { method: "POST" },
    ),

  /** 客户端设置：登记键当前值 + 格式指纹 + 备份点。 */
  clientSettings: () => request<ClientSettingsSnapshot>("/api/client/settings"),

  /** 写入客户端设置：只允许登记的键；指纹不匹配即被拒绝。 */
  writeClientSettings: (
    updates: Record<string, string | boolean>,
    confirmFingerprint: string,
  ) =>
    request<ClientSettingsWriteResult>("/api/client/settings", {
      method: "PUT",
      body: JSON.stringify({ updates, confirmFingerprint }),
    }),

  /** 用备份点还原客户端设置文件。 */
  restoreClientSettings: (backupId: string) =>
    request<ClientSettingsSnapshot>("/api/client/settings/restore", {
      method: "POST",
      body: JSON.stringify({ backupId }),
    }),

  /** 概览「待处理」清单（只读聚合，四类来源合成一张表）。 */
  overviewActionables: () =>
    request<{ items: ActionableItem[]; pulse: OverviewPulse }>("/api/overview/actionables"),

  /** 自动轮换策略与台账（`schedules.json` 的 rotation 段）。 */
  rotation: () => request<RotationState>("/api/rotation"),

  /** 改自动轮换阈值：省略的字段保持原值。 */
  updateRotation: (patch: Partial<RotationPolicy>) =>
    request<RotationState>("/api/rotation", { method: "PUT", body: JSON.stringify(patch) }),

  /** 立即按策略评估一轮；`dryRun=true` 只推演，绝不写客户端。 */
  runRotation: (dryRun = false) =>
    request<{
      skipped: string | null;
      decision: RotationDecision | null;
      executed: boolean;
      detail: string;
      clientRunning: boolean;
      state: RotationState;
    }>(`/api/rotation/run-now${dryRun ? "?dryRun=true" : ""}`, { method: "POST" }),

  accounts: (scope: string) =>
    request<{ accounts: Account[] }>(`/api/accounts?scope=${encodeURIComponent(scope)}`),

  /** 合并号池：所有已实现作用域的账号合成一张表，平台在 account.platform 里。 */
  mergedAccounts: () => request<{ accounts: Account[] }>("/api/accounts/merged"),

  /** 各账号积分余额与到期时间（按 accountId 索引）。refresh=true 绕过缓存。 */
  credits: (refresh = false, accountIds?: string[]) => {
    const params = new URLSearchParams();
    if (refresh) params.set("refresh", "true");
    if (accountIds?.length) params.set("accountIds", accountIds.join(","));
    const query = params.toString();
    return request<{ credits: Record<string, CreditInfo> }>(
      `/api/accounts/credits${query ? `?${query}` : ""}`,
    );
  },

  /** 活动检测：每个账号当前能做什么。refresh=true 绕过缓存。 */
  activities: (refresh = false, accountIds?: string[]) => {
    const params = new URLSearchParams();
    if (refresh) params.set("refresh", "true");
    if (accountIds?.length) params.set("accountIds", accountIds.join(","));
    const query = params.toString();
    return request<{ accounts: AccountActivities[]; summary: ActivitySummary }>(
      `/api/activities${query ? `?${query}` : ""}`,
    );
  },

  /** 活动目录：静态清单，零网络。页面先用它渲染骨架卡片，再被检测结果覆盖。 */
  activityCatalog: () => request<{ entries: ActivityCatalogEntry[] }>("/api/activities/catalog"),

  /** 一键执行可自动完成的活动；已完成的后端会自动跳过。 */
  runActivities: (accountIds?: string[]) =>
    request<ActivityRunReport>("/api/activities/run", {
      method: "POST",
      body: JSON.stringify({ accountIds: accountIds ?? null }),
    }),

  /** 各账号请求与积分消耗的合并视图。refresh=true 绕过缓存。 */
  usage: (refresh = false, accountIds?: string[]) => {
    const params = new URLSearchParams();
    if (refresh) params.set("refresh", "true");
    if (accountIds?.length) params.set("accountIds", accountIds.join(","));
    const query = params.toString();
    return request<{ accounts: AccountUsage[]; summary: UsageSummary }>(
      `/api/usage${query ? `?${query}` : ""}`,
    );
  },

  /** 一次计费请求在本机还原出的对话（requestId 即本机 conversationRequestId）。 */
  usageRequestTurn: (accountId: string, requestId: string) =>
    request<UsageRequestTurn>(
      `/api/usage/requests/${encodeURIComponent(requestId)}?accountId=${encodeURIComponent(accountId)}`,
    ),

  /** 批量取一页明细的对话摘要（首条提问 + 末条回答）。 */
  usageTurns: (accountId: string, requestIds: string[]) => {
    const params = new URLSearchParams({ accountId });
    if (requestIds.length) params.set("ids", requestIds.join(","));
    return request<{ accountId: string; turns: Record<string, UsageTurnBrief> }>(
      `/api/usage/turns?${params.toString()}`,
    );
  },

  /** 读某账号下某条会话的完整对话正文（含归属校验）。 */
  conversationMessages: (accountId: string, conversationId: string) =>
    request<{
      accountId: string;
      conversationId: string;
      messages: ConversationMessage[];
    }>(
      `/api/sessions/${encodeURIComponent(accountId)}/${encodeURIComponent(
        conversationId,
      )}/messages`,
    ),

  importAccount: () =>
    request<{ created: boolean; restored: boolean; account: Account }>("/api/accounts/import", {
      method: "POST",
      body: JSON.stringify({}),
    }),

  /** JSON 粘贴导入：内容自动识别平台，不需要选。 */
  importJsonAccount: (content: string) =>
    request<{ created: boolean; restored: boolean; account: Account }>("/api/accounts/import-json", {
      method: "POST",
      body: JSON.stringify({ content }),
    }),

  /** 导出选中账号（含登录凭据）。空选择后端会返回 400。 */
  exportAccounts: (accountIds: string[]) =>
    request<AccountBundle>("/api/accounts/export", {
      method: "POST",
      body: JSON.stringify({ accountIds }),
    }),

  /** 导入本工具导出的账号包；逐条处理，坏条目只记进 errors。 */
  importAccountBundle: (content: string) =>
    request<AccountBundleImportResult>("/api/accounts/import-bundle", {
      method: "POST",
      body: JSON.stringify({ content }),
    }),

  oauthStart: () =>
    request<{ loginId: string; verificationUri: string; expiresIn: number }>("/api/oauth/start", {
      method: "POST",
    }),

  oauthStatus: (loginId: string) =>
    request<{ done: boolean; result?: { created: boolean; restored: boolean; account: Account }; error?: string }>(
      `/api/oauth/status?loginId=${encodeURIComponent(loginId)}`,
    ),

  /** Trae 扫码登录：发起（后端读 storage.json 设备身份 + 占 17388 端口）。 */
  traeOauthStart: () =>
    request<{ loginId: string; verificationUri: string; expiresIn: number; port: number }>(
      "/api/trae/oauth/start",
      { method: "POST" },
    ),

  /** Trae 扫码登录：轮询结果。 */
  traeOauthStatus: (loginId: string) =>
    request<{ done: boolean; result?: { created: boolean; restored: boolean; account: Account }; error?: string }>(
      `/api/trae/oauth/status?loginId=${encodeURIComponent(loginId)}`,
    ),

  /** Trae 扫码登录：取消（释放 17388 端口）。 */
  traeOauthCancel: (loginId: string) =>
    request<{ ok: boolean }>(`/api/trae/oauth/cancel?loginId=${encodeURIComponent(loginId)}`, {
      method: "POST",
    }),

  updateNote: (accountId: string, note: string) =>
    request<Account>(`/api/accounts/${accountId}/note`, {
      method: "PATCH",
      body: JSON.stringify({ note }),
    }),

  deleteAccount: (accountId: string) =>
    request<{ ok: boolean }>(`/api/accounts/${accountId}`, { method: "DELETE" }),

  dryRun: (accountId: string, scope?: string, slot?: string) =>
    request<SwitchReport>("/api/switch/dry-run", {
      method: "POST",
      body: JSON.stringify({ scope, accountId, slot }),
    }),

  /** `slot` 不传 = 该平台默认位；只读 / 未适配的位会被后端拒绝。 */
  switchAccount: (accountId: string, scope?: string, relaunch = true, slot?: string) =>
    request<SwitchReport>("/api/switch", {
      method: "POST",
      body: JSON.stringify({ scope, accountId, relaunch, slot }),
    }),

  /** 切换前迁移预检（只读推演，不写任何文件）。 */
  migrationPreview: (accountId: string, scope: string) => {
    const params = new URLSearchParams();
    params.set("scope", scope);
    params.set("accountId", accountId);
    return request<MigrationPreview>(`/api/migration/preview?${params.toString()}`);
  },

  backups: (scope: string) =>
    request<{ snapshots: Snapshot[] }>(`/api/backups?scope=${encodeURIComponent(scope)}`),

  createBackup: (scope: string, note = "") =>
    request<Snapshot>("/api/backups", {
      method: "POST",
      body: JSON.stringify({ scope, note }),
    }),

  verifyBackup: (snapshotId: string) =>
    request<{ id: string; verified: boolean }>(`/api/backups/${snapshotId}/verify`, {
      method: "POST",
    }),

  /** 用快照覆盖回客户端目录；返回恢复前自动落的那份安全快照 id（目录为空时为 null）。 */
  restoreBackup: (snapshotId: string) =>
    request<{ snapshotId: string; safetySnapshotId: string | null }>(
      `/api/backups/${snapshotId}/restore`,
      { method: "POST" },
    ),

  /** 全局备份：分区清单（含「是否含凭据」）+ 已有备份点。 */
  globalBackups: (limit = 50) =>
    request<GlobalBackupOverview>(`/api/backups/global?limit=${limit}`),

  /** 按分区备份本工具自己的数据；勾了含凭据的分区必须传 confirmCredentials=true。 */
  createGlobalBackup: (parts: string[], note = "", confirmCredentials = false) =>
    request<Snapshot>("/api/backups/global", {
      method: "POST",
      body: JSON.stringify({ parts, note, confirmCredentials }),
    }),

  /** 网关状态：配置 + 是否在监听 + 计数 + 模型目录。 */
  gatewayStatus: () => request<GatewayStatus>("/api/gateway/status"),

  /** 改网关配置（局部更新，改完立即生效：按下开关就会起停网关端口）。 */
  gatewayConfig: (patch: GatewayConfigPatch) =>
    request<GatewayStatus>("/api/gateway/config", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  gatewayKeys: () => request<{ keys: GatewayKey[] }>("/api/gateway/keys"),

  /** 签发 Key：明文只在这次响应里出现，弹窗关掉就再也看不到。 */
  createGatewayKey: (options: {
    label: string;
    scope?: string;
    capabilities?: string[];
    models?: string[];
  }) =>
    request<{ key: string; record: GatewayKey }>("/api/gateway/keys", {
      method: "POST",
      body: JSON.stringify(options),
    }),

  /** 改 Key 的备注 / 模型范围：Key 与明文不变，所以不必重签、客户端不用改配置。 */
  updateGatewayKey: (
    keyId: string,
    patch: { label?: string; scope?: string; capabilities?: string[]; models?: string[] },
  ) =>
    request<{ record: GatewayKey }>(`/api/gateway/keys/${keyId}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  /** 吊销：停用这把 Key，但保留记录与用量。 */
  revokeGatewayKey: (keyId: string) =>
    request<{ record: GatewayKey }>(`/api/gateway/keys/${keyId}/revoke`, { method: "POST" }),

  /** 恢复：把已吊销的 Key 重新变成有效（记录与用量都在）。 */
  restoreGatewayKey: (keyId: string) =>
    request<{ record: GatewayKey }>(`/api/gateway/keys/${keyId}/restore`, { method: "POST" }),

  /** 删除：把记录从列表里彻底清掉（只能删已吊销的 Key）。 */
  deleteGatewayKey: (keyId: string) =>
    request<{ record: GatewayKey }>(`/api/gateway/keys/${keyId}`, { method: "DELETE" }),

  /** 单把 Key 的用量分解（最近请求 + 按模型 + 按小时）。 */
  gatewayKeyUsage: (keyId: string, limit = 20) =>
    request<GatewayKeyUsage>(
      `/api/gateway/keys/${encodeURIComponent(keyId)}/usage?limit=${limit}`,
    ),

  /** 最近的请求日志；`filters` 可只看某把 Key / 某个客户端传入的会话标识。 */
  gatewayLogs: (limit = 50, filters: { keyId?: string; sessionId?: string } = {}) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (filters.keyId) params.set("keyId", filters.keyId);
    if (filters.sessionId) params.set("sessionId", filters.sessionId);
    return request<{ logs: GatewayLogEntry[] }>(`/api/gateway/logs?${params.toString()}`);
  },

  clearGatewayLogs: () =>
    request<{ removed: number }>("/api/gateway/logs", { method: "DELETE" }),

  /** 强制刷新上游模型目录（绕过 10 分钟缓存）。 */
  refreshGatewayModels: () =>
    request<{ models: GatewayCatalogModel[]; fresh: boolean }>("/api/gateway/models/refresh", {
      method: "POST",
    }),

  /** 账号池状态：每账号可用性、冷却剩余、积分（只读缓存）与人工处置。 */
  gatewayPool: () => request<GatewayPool>("/api/gateway/pool"),

  /** 排除 / 恢复某账号；或设为 / 取消「指定账号偏好」（两个字段都可选，只传要动的那项）。 */
  updateGatewayPoolAccount: (
    accountId: string,
    patch: { excluded?: boolean; preferred?: boolean },
  ) =>
    request<GatewayPool>(`/api/gateway/pool/accounts/${accountId}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  /** 探测某账号：成功即解除它的冷却，结果同时进请求日志。 */
  probeGatewayAccount: (accountId: string, model: string) =>
    request<GatewayProbeResult>("/api/gateway/pool/probe", {
      method: "POST",
      body: JSON.stringify({ accountId, model }),
    }),

  // 文档与接口清单都是后端运行期读出来的，新增 md 或新增路由后刷新即可见。
  docs: () => request<{ root: string | null; files: DocEntry[] }>("/api/docs"),

  docContent: (path: string) =>
    request<DocContent>(`/api/docs/content?path=${encodeURIComponent(path)}`),


  /** 接口文档是否可用：前端先问这里，再决定要不要挂 iframe。 */
  docsStatus: () => request<DocsStatus>("/api/docs/status"),

  logFiles: () => request<{ dir: string; files: LogFile[] }>("/api/logs/files"),

  logs: (query: LogQuery) => {
    const params = new URLSearchParams();
    params.set("page", String(query.page ?? 1));
    params.set("page_size", String(query.pageSize ?? 50));
    if (query.level) params.set("level", query.level);
    if (query.keyword) params.set("keyword", query.keyword);
    if (query.file) params.set("file", query.file);
    return request<LogPage>(`/api/logs?${params.toString()}`);
  },
};

export function formatTime(iso: string | null): string {
  if (!iso) return t("api.never");
  const date = new Date(iso);
  const diff = Date.now() - date.getTime();
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return t("api.justNow");
  if (diff < hour) return t("api.minutesAgo", { count: Math.floor(diff / minute) });
  if (diff < day) return t("api.hoursAgo", { count: Math.floor(diff / hour) });
  if (diff < 30 * day) return t("api.daysAgo", { count: Math.floor(diff / day) });
  return date.toLocaleDateString();
}

/**
 * 到期时间展示：unix 秒 → 「剩 3 天」/「今天到期」/「已过期」/ 具体日期。
 * 积分到期是用户决策的关键信息，必须一眼看出紧迫度。
 */
export function formatExpiry(epochSeconds: number | null): string {
  if (!epochSeconds) return t("api.noExpiry");
  const diffMs = epochSeconds * 1000 - Date.now();
  const day = 24 * 3600 * 1000;
  if (diffMs <= 0) return t("api.expired");
  if (diffMs < day) return t("api.expiresToday");
  const days = Math.floor(diffMs / day);
  if (days <= 30) return t("api.daysLeft", { days });
  return new Date(epochSeconds * 1000).toLocaleDateString();
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 积分 / 余额这类金额数字的展示：统一精度，去掉浮点累加误差与多余尾随零。
 *
 * - 先四舍五入到 2 位小数，抹平 `7154.650000000001` 这种累加误差；
 * - 整数（`432.0` → `432`）不显示小数；
 * - 非整数最多保留 2 位，并去掉尾随零（`1448.60` → `1448.6`，`1448.62` → `1448.62`）。
 */
export function formatAmount(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  if (Number.isInteger(rounded)) return String(rounded);
  return String(parseFloat(rounded.toFixed(2)));
}
