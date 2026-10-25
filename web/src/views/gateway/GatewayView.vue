<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Loader2, RefreshCw, TriangleAlert } from "lucide-vue-next";
import StatusPill from "../../components/StatusPill.vue";
import SelectMenu from "../../components/SelectMenu.vue";
import { t } from "../../lib/i18n";
import KeyUsageDrawer from "./KeyUsageDrawer.vue";
import {
  GATEWAY_ROUTE,
  GATEWAY_TABS,
  findGatewayTab,
  resolveGatewayKey,
} from "./tabs";
import { createGatewayStore, provideGateway } from "./useGateway";

/**
 * 网关容器。左栏由壳层渲染（`GatewayRail`），这里只管右侧内容。
 *
 * 形态来自 `route.meta.immersive = 'shell'`，不存本地 state ——
 * 否则刷新页面布局就丢了。共享状态在这里建一份，子 tab 用 `useGateway()` 取。
 */
const route = useRoute();
const router = useRouter();

const store = createGatewayStore();
provideGateway(store);

const activeKey = computed(() => resolveGatewayKey(route.query.tab));
const activeTab = computed(() => findGatewayTab(activeKey.value) ?? GATEWAY_TABS[0]);

// 非法 ?tab= 归一成默认项，别让地址栏留着一个查无此项的值。
watch(
  activeKey,
  (key) => {
    if (route.query.tab !== key) {
      void router.replace({ path: GATEWAY_ROUTE, query: { ...route.query, tab: key } });
    }
  },
  { immediate: true },
);

/**
 * 自动刷新：网关页内配置，存本地偏好（localStorage）。
 * 刷新内容 = 状态 / Key 列表 / 请求日志 / 账号池（一次 `load()` 全量覆盖）。
 * 只重拉数据、不改表单草稿；用户正在改名 / 编辑范围 / 打开用量抽屉时不打断。
 */
const REFRESH_KEY = "chaierww.gateway-refresh-seconds";
const refreshOptions = computed(() => [
  { value: 0, label: t("gateway.refreshOff") },
  { value: 30, label: t("gateway.refresh30") },
  { value: 60, label: t("gateway.refresh60") },
  { value: 300, label: t("gateway.refresh300") },
]);
const refreshSeconds = ref(Number(localStorage.getItem(REFRESH_KEY) ?? 0));
let refreshTimer: number | null = null;

function applyRefreshTimer(): void {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (refreshSeconds.value > 0) {
    refreshTimer = window.setInterval(() => {
      // 网关页自动刷新：不打断改名 / 编辑范围 / 用量抽屉这些正在进行的动作。
      if (store.renaming === null && store.editing === null && store.keyUsage === null) {
        void store.load();
      }
    }, refreshSeconds.value * 1000);
  }
}

watch(refreshSeconds, (value) => {
  localStorage.setItem(REFRESH_KEY, String(value));
  applyRefreshTimer();
});

onMounted(() => {
  void store.load();
  applyRefreshTimer();
});

onBeforeUnmount(() => {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
});
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <header class="flex shrink-0 flex-wrap items-start justify-between gap-3 pb-4">
      <div>
        <h2 class="text-title font-semibold tracking-tight text-ink">{{ t(activeTab.labelKey) }}</h2>
        <p class="mt-0.5 text-body-sm text-muted">{{ t(activeTab.hintKey) }}</p>
      </div>
      <div class="flex items-center gap-2">
        <StatusPill :tone="store.status?.running ? 'brand' : 'neutral'" dot>
          {{
            store.status?.running
              ? t("gateway.listening")
              : store.status?.enabled
                ? t("gateway.startFailed")
                : t("common.off")
          }}
        </StatusPill>
        <SelectMenu
          v-model="refreshSeconds"
          :options="refreshOptions"
          :title="t('gateway.refreshInterval')"
          size="sm"
          class="w-[128px]"
        />
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-2 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-60"
          :disabled="store.loading"
          @click="store.load"
        >
          <RefreshCw :size="14" :stroke-width="1.9" :class="store.loading ? 'animate-spin' : ''" />
          {{ t("common.refresh") }}
        </button>
      </div>
    </header>

    <div
      v-if="store.error"
      class="mb-3 flex items-start gap-2.5 rounded-card border border-line bg-danger-soft px-4 py-2.5 text-body-sm text-danger"
    >
      <TriangleAlert :size="15" class="mt-0.5 shrink-0" />
      <div>{{ store.error }}</div>
    </div>

    <div
      v-if="store.loading && !store.status"
      class="flex items-center gap-2 px-5 py-10 text-body text-muted"
    >
      <Loader2 :size="15" class="animate-spin" /> {{ t("gateway.loading") }}
    </div>

    <div v-else class="min-h-0 flex-1 overflow-y-auto pr-1">
      <KeepAlive>
        <component :is="activeTab.component" />
      </KeepAlive>
    </div>

    <!-- 单把 Key 的用量抽屉挂在容器上：Key 页与概览页都能打开同一份视图。 -->
    <KeyUsageDrawer />
  </div>
</template>
