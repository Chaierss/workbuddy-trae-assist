/**
 * 健康中心文案。
 *
 * 事件标题 / 明细由后端按本地事实生成（与概览待处理同一口径），这里只放
 * 页面骨架、状态名、筛选与按钮文案；不要在前端再写一份事件标题。
 */
export const healthZh = {
  "nav.health": "健康中心",

  "health.title": "健康中心",
  "health.subtitle":
    "上游结构、登录态、号池供给、活动与计划任务的异常都在这里收成一条事件流；能处置的直接处置，处理完信号消失会自动归档。",
  "health.refresh": "刷新事件",
  "health.refreshed": "已刷新：{time}",
  "health.loadError": "事件流读取失败：{error}",

  "health.summary.unhandled": "未处理",
  "health.summary.high": "高优先",
  "health.summary.warn": "需关注",
  "health.summary.snoozed": "稍后提醒",
  "health.summary.resolved": "已解决",

  "health.filter.active": "未解决",
  "health.filter.resolved": "已解决",
  "health.filter.all": "全部",
  "health.filter.kind": "类别",
  "health.filter.allKinds": "全部类别",

  "health.kind.compat_changed": "上游结构",
  "health.kind.login_invalid": "登录失效",
  "health.kind.login_blocked": "设备绑定",
  "health.kind.login_expiring": "登录到期",
  "health.kind.credential_expired": "凭据过期",
  "health.kind.credential_expiring": "凭据到期",
  "health.kind.credential_stalled": "续期失败",
  "health.kind.credential_relogin": "需要重登",
  "health.kind.pool_supply": "号池供给",
  "health.kind.activity_failed": "活动失败",
  "health.kind.schedule_failed": "计划任务",

  "health.severity.high": "高",
  "health.severity.warn": "中",
  "health.severity.info": "低",

  "health.state.open": "待处理",
  "health.state.acked": "已确认",
  "health.state.snoozed": "稍后提醒",
  "health.state.resolved": "已解决",

  "health.action.ack": "确认",
  "health.action.snooze": "稍后提醒",
  "health.action.unsnooze": "取消稍后",
  "health.action.detail": "处置记录",
  "health.action.bulkAck": "批量确认",
  "health.action.bulkSnooze": "批量稍后提醒",
  "health.action.selectAll": "全选未解决",
  "health.action.selected": "已选 {count} 条",
  "health.action.clear": "清空选择",
  "health.action.running": "执行中…",

  "health.snooze.1h": "1 小时",
  "health.snooze.24h": "24 小时",
  "health.snooze.72h": "3 天",

  "health.done": "处置完成：{message}",
  "health.bulkDone": "已处理 {done} 条，跳过 {skipped} 条",
  "health.actionError": "处置失败：{error}",

  "health.evidence.title": "判断依据",
  "health.evidence.account": "账号",
  "health.evidence.expiresAt": "到期时间",
  "health.evidence.failures": "连续失败",
  "health.evidence.fingerprint": "结构指纹",
  "health.evidence.version": "客户端版本",
  "health.evidence.task": "计划任务",
  "health.evidence.activity": "活动",
  "health.evidence.healthy": "健康账号",
  "health.evidence.creditsDays": "库存天数",
  "health.evidence.none": "暂无更多依据",

  "health.audit.title": "处置记录",
  "health.audit.empty": "还没有处置记录",
  "health.audit.auto": "自动",

  "health.empty.active.title": "没有待处理的异常",
  "health.empty.active.desc": "上游结构、登录态、号池供给与活动都正常；有变化会自动出现在这里。",
  "health.empty.resolved.title": "还没有已解决事件",
  "health.empty.resolved.desc": "信号消失后事件会自动归档，保留最近 30 天。",

  "overview.health.title": "健康中心有 {count} 条待处理",
  "overview.health.desc": "上游结构、登录态、号池供给与活动异常集中在这里，点进去可以逐条处置。",
  "overview.health.cta": "打开健康中心",

  "settings.health.title": "健康中心自动处置",
  "settings.health.desc": "默认关闭：这两项都会实际打上游，开启前请确认号池状态。",
  "settings.health.refreshTitle": "到期自动续期",
  "settings.health.refreshDesc": "对已过期或 24 小时内到期的号自动续期一次；每轮最多 3 个，避免集中触发风控。",
  "settings.health.rerunTitle": "失败活动自动补跑",
  "settings.health.rerunDesc": "当天确有失败的活动，在下一轮计划任务里自动重跑一次；只重跑失败项。",

  "schedule.healthAutoTitle": "健康自动处置",
  "schedule.healthAutoDesc": "按常规设置里的两个开关，自动续期到期号与补跑失败活动。",
  "schedule.upstreamTitle": "上游结构探测",
  "schedule.upstreamDesc": "只读记录客户端版本与登录态结构指纹，结构变化时提示确认。",
  "schedule.poolTitle": "号池供给聚合",
  "schedule.poolDesc": "纯本地计算健康数与库存天数，低于阈值时在概览提示补给。",
  "schedule.sessionTitle": "会话镜像采集",
  "schedule.sessionDesc": "只读采集当前登录号的会话镜像，不写客户端。",
};

export const healthEn: Record<keyof typeof healthZh, string> = {
  "nav.health": "Health",

  "health.title": "Health center",
  "health.subtitle":
    "Upstream structure, login state, pool supply, activities and scheduled tasks all feed one event stream. Act on what you can; events archive automatically once the signal clears.",
  "health.refresh": "Refresh",
  "health.refreshed": "Refreshed at {time}",
  "health.loadError": "Could not load events: {error}",

  "health.summary.unhandled": "Unhandled",
  "health.summary.high": "High priority",
  "health.summary.warn": "Needs attention",
  "health.summary.snoozed": "Snoozed",
  "health.summary.resolved": "Resolved",

  "health.filter.active": "Unresolved",
  "health.filter.resolved": "Resolved",
  "health.filter.all": "All",
  "health.filter.kind": "Category",
  "health.filter.allKinds": "All categories",

  "health.kind.compat_changed": "Upstream structure",
  "health.kind.login_invalid": "Login invalid",
  "health.kind.login_blocked": "Device binding",
  "health.kind.login_expiring": "Login expiring",
  "health.kind.credential_expired": "Credential expired",
  "health.kind.credential_expiring": "Credential expiring",
  "health.kind.credential_stalled": "Refresh failing",
  "health.kind.credential_relogin": "Re-login needed",
  "health.kind.pool_supply": "Pool supply",
  "health.kind.activity_failed": "Activity failed",
  "health.kind.schedule_failed": "Scheduled task",

  "health.severity.high": "High",
  "health.severity.warn": "Medium",
  "health.severity.info": "Low",

  "health.state.open": "Open",
  "health.state.acked": "Acknowledged",
  "health.state.snoozed": "Snoozed",
  "health.state.resolved": "Resolved",

  "health.action.ack": "Acknowledge",
  "health.action.snooze": "Snooze",
  "health.action.unsnooze": "Unsnooze",
  "health.action.detail": "Action log",
  "health.action.bulkAck": "Acknowledge selected",
  "health.action.bulkSnooze": "Snooze selected",
  "health.action.selectAll": "Select all unresolved",
  "health.action.selected": "{count} selected",
  "health.action.clear": "Clear selection",
  "health.action.running": "Running…",

  "health.snooze.1h": "1 hour",
  "health.snooze.24h": "24 hours",
  "health.snooze.72h": "3 days",

  "health.done": "Done: {message}",
  "health.bulkDone": "Handled {done}, skipped {skipped}",
  "health.actionError": "Action failed: {error}",

  "health.evidence.title": "Evidence",
  "health.evidence.account": "Account",
  "health.evidence.expiresAt": "Expires at",
  "health.evidence.failures": "Consecutive failures",
  "health.evidence.fingerprint": "Structure fingerprint",
  "health.evidence.version": "Client version",
  "health.evidence.task": "Scheduled task",
  "health.evidence.activity": "Activity",
  "health.evidence.healthy": "Healthy accounts",
  "health.evidence.creditsDays": "Days of stock",
  "health.evidence.none": "No further evidence",

  "health.audit.title": "Action log",
  "health.audit.empty": "No actions yet",
  "health.audit.auto": "Automatic",

  "health.empty.active.title": "Nothing needs attention",
  "health.empty.active.desc":
    "Upstream structure, login state, pool supply and activities look healthy. New changes will appear here.",
  "health.empty.resolved.title": "No resolved events yet",
  "health.empty.resolved.desc":
    "Events archive automatically once their signal clears; the last 30 days are kept.",

  "overview.health.title": "{count} health events need attention",
  "overview.health.desc":
    "Upstream structure, login state, pool supply and activity issues are collected here. Open the health center to handle them one by one.",
  "overview.health.cta": "Open health center",

  "settings.health.title": "Automatic remediation",
  "settings.health.desc":
    "Off by default: both actions hit the upstream. Confirm your pool state before enabling.",
  "settings.health.refreshTitle": "Refresh expiring credentials",
  "settings.health.refreshDesc":
    "Refresh credentials that expired or expire within 24 hours, up to 3 accounts per run to avoid rate limits.",
  "settings.health.rerunTitle": "Re-run failed activities",
  "settings.health.rerunDesc":
    "Failed activities from today are retried once during the next scheduled run; only failed items are retried.",

  "schedule.healthAutoTitle": "Automatic remediation",
  "schedule.healthAutoDesc":
    "Refresh expiring credentials and retry failed activities according to the two switches in General settings.",
  "schedule.upstreamTitle": "Upstream structure probe",
  "schedule.upstreamDesc":
    "Read-only record of client version and login-state structure; reports changes for confirmation.",
  "schedule.poolTitle": "Pool supply aggregation",
  "schedule.poolDesc":
    "Local-only aggregation of healthy accounts and days of stock; warns when below thresholds.",
  "schedule.sessionTitle": "Session mirror capture",
  "schedule.sessionDesc":
    "Read-only capture of the currently signed-in account's session mirror; never writes the client.",
};
