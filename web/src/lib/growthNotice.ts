/**
 * 成长任务的风险告知。
 *
 * 活动执行（`POST /activities/run`）除了签到，还会对账号里可自动完成的成长任务
 * 通过接口标记完成 —— 那不是用户客户端里真实发生的操作。这条链路与「代点一次签到」
 * 性质不同，被平台主张为不当获利时防御力更弱，所以第一次跑之前要让用户知情。
 *
 * 只确认一次：记在 localStorage，之后不再打扰。隐私模式下 localStorage 读写会抛错，
 * 这里全部吞掉 —— 大不了下次再提示一遍，不能因为存储不可用就阻断功能。
 *
 * 之所以抽成模块：活动页（ActivityView）和概览页快捷操作（OverviewView）是两个
 * 独立入口，同一份告知必须共用同一个 ack 标记，否则用户会在两个页面各被问一次。
 */
const KEY = "chaierww.growthNoticeAck";

export function growthNoticeAcknowledged(): boolean {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function acknowledgeGrowthNotice(): void {
  try {
    localStorage.setItem(KEY, "1");
  } catch {
    /* 写不进去就下次再提示，不阻断执行 */
  }
}
