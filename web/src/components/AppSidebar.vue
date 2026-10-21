<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";
import {
  Activity,
  FileText,
  LayoutDashboard,
  Network,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-vue-next";
import { accountCount, health } from "../stores/app";

const route = useRoute();

// /docs 与 /docs/<path> 是两个路由记录，这里统一按前缀判断高亮。
const docsActive = computed(() => route.path.startsWith("/docs"));

const items = computed(() => [
  { to: "/overview", label: "概览", icon: LayoutDashboard, badge: 0 },
  { to: "/accounts", label: "号池", icon: Users, badge: accountCount.value },
  { to: "/activity", label: "活动", icon: Sparkles, badge: 0 },
  { to: "/usage", label: "用量", icon: Activity, badge: 0 },
  { to: "/gateway", label: "网关", icon: Network, badge: 0 },
]);

const version = "v0.1.0";
</script>

<template>
  <aside class="flex w-[226px] shrink-0 flex-col border-r border-line bg-surface">
    <div class="flex h-16 shrink-0 items-center gap-3 border-b border-line-soft px-4">
      <div class="grid size-9 shrink-0 place-items-center rounded-[11px] bg-brand text-white">
        <ShieldCheck :size="19" :stroke-width="2" />
      </div>
      <div class="min-w-0">
        <div class="truncate text-[14px] font-semibold tracking-tight">chaierww</div>
        <div class="truncate text-[11px] text-muted">本地多账号</div>
      </div>
    </div>

    <nav class="flex-1 space-y-0.5 overflow-y-auto px-2.5 py-3">
      <RouterLink
        v-for="item in items"
        :key="item.to"
        v-slot="{ navigate, isActive }"
        :to="item.to"
        custom
      >
        <button
          type="button"
          class="flex w-full items-center gap-2.5 rounded-[10px] px-3 py-2 text-[13px] transition-colors"
          :class="
            isActive
              ? 'bg-brand-soft font-medium text-brand-dark'
              : 'text-ink-soft hover:bg-line-soft hover:text-ink'
          "
          @click="navigate"
        >
          <component :is="item.icon" :size="16" :stroke-width="1.9" class="shrink-0" />
          <span class="flex-1 truncate text-left">{{ item.label }}</span>
          <span
            v-if="item.badge"
            class="rounded-full px-1.5 py-0.5 text-[11px] leading-none"
            :class="isActive ? 'bg-white/80 text-brand-dark' : 'bg-line-soft text-muted'"
          >
            {{ item.badge }}
          </span>
        </button>
      </RouterLink>

      <div class="!my-2 h-px bg-line-soft" />

      <RouterLink v-slot="{ navigate }" to="/docs" custom>
        <button
          type="button"
          class="flex w-full items-center gap-2.5 rounded-[10px] px-3 py-2 text-[13px] transition-colors"
          :class="
            docsActive
              ? 'bg-brand-soft font-medium text-brand-dark'
              : 'text-ink-soft hover:bg-line-soft hover:text-ink'
          "
          @click="navigate"
        >
          <FileText :size="16" :stroke-width="1.9" class="shrink-0" />
          <span class="flex-1 truncate text-left">文档</span>
        </button>
      </RouterLink>
    </nav>

    <!-- 设置固定在左下角，与主导航分开：它是全局配置，不属于内容浏览 -->
    <div class="shrink-0 border-t border-line-soft px-2.5 py-2.5">
      <RouterLink v-slot="{ navigate }" to="/settings" custom>
        <button
          type="button"
          class="flex w-full items-center gap-2.5 rounded-[10px] px-3 py-2 text-[13px] text-ink-soft transition-colors hover:bg-line-soft hover:text-ink"
          @click="navigate"
        >
          <Settings :size="16" :stroke-width="1.9" class="shrink-0" />
          <span class="flex-1 truncate text-left">设置</span>
        </button>
      </RouterLink>
      <div class="truncate px-3 pt-1.5 text-[11px] text-muted" :title="health?.dataDir">
        {{ version }} · 数据留在本机
      </div>
    </div>
  </aside>
</template>
