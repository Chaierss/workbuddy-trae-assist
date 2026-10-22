<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { t } from "../../lib/i18n";
import { GATEWAY_ROUTE, GATEWAY_TABS, resolveGatewayKey } from "./tabs";

/**
 * 壳层在 `shell` 态渲染这个左栏（240px），网关页自己只负责右侧内容。
 *
 * 这里**不能**用 `useGateway()`：左栏由 `AppShell` 渲染，挂在 `GatewayView` 的
 * provide 作用域之外，取不到那份 store（会直接抛「未注入」）。因此左栏只做导航，
 * 运行状态胶囊放在容器 header 里展示。
 */
const route = useRoute();
const router = useRouter();

const activeKey = computed(() => resolveGatewayKey(route.query.tab));

function openTab(key: string): void {
  // replace：切 tab 不该把浏览器的后退栈塞满。
  void router.replace({ path: GATEWAY_ROUTE, query: { tab: key } });
}

function backToMain(): void {
  void router.push("/overview");
}
</script>

<template>
  <aside
    class="fixed inset-y-0 left-0 z-[200] flex w-[var(--sider-width)] flex-col border-r border-line bg-surface"
  >
    <button
      type="button"
      class="block w-full border-b border-line-soft px-4 pb-3 pt-5 text-left transition-colors hover:bg-line-soft"
      @click="backToMain"
    >
      <h1 class="text-[13px] font-semibold tracking-tight text-ink">{{ t("gateway.shell.title") }}</h1>
      <p class="mt-0.5 text-[11.5px] text-muted">{{ t("gateway.shell.subtitle") }}</p>
    </button>

    <nav class="flex-1 space-y-0.5 overflow-y-auto px-2 pb-3 pt-3">
      <button
        v-for="tab in GATEWAY_TABS"
        :key="tab.key"
        type="button"
        class="flex w-full items-start gap-2.5 rounded-[9px] px-2.5 py-2 text-left transition-colors"
        :class="
          activeKey === tab.key
            ? 'bg-brand-soft text-brand-dark'
            : 'text-ink-soft hover:bg-line-soft hover:text-ink'
        "
        @click="openTab(tab.key)"
      >
        <component :is="tab.icon" :size="15" :stroke-width="1.9" class="mt-0.5 shrink-0" />
        <span class="min-w-0 flex-1">
          <span class="block truncate text-[12.5px]" :class="activeKey === tab.key ? 'font-medium' : ''">
            {{ t(tab.labelKey) }}
          </span>
          <span class="mt-0.5 block text-[11px] leading-snug text-muted">{{ t(tab.hintKey) }}</span>
        </span>
      </button>
    </nav>
  </aside>
</template>
