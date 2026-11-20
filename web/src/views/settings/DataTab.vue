<script setup lang="ts">
import { computed, ref, type Component } from "vue";
import { Database, ScrollText } from "lucide-vue-next";
import BackupTab from "./BackupTab.vue";
import LogsTab from "./LogsTab.vue";
import { t, type MessageKey } from "../../lib/i18n";

/**
 * 设置 → 数据与备份：备份 / 恢复与运行日志。
 * 两者都是「出问题时才打开」，合在一页，主设置只留常用项。
 */
interface DataTab {
  key: string;
  labelKey: MessageKey;
  hintKey: MessageKey;
  icon: Component;
  component: Component;
}

const tabs: DataTab[] = [
  {
    key: "backup",
    labelKey: "settings.backup.label",
    hintKey: "settings.backup.hint",
    icon: Database,
    component: BackupTab,
  },
  {
    key: "logs",
    labelKey: "settings.logs.label",
    hintKey: "settings.logs.hint",
    icon: ScrollText,
    component: LogsTab,
  },
];

const activeKey = ref(tabs[0].key);
const active = computed(() => tabs.find((tab) => tab.key === activeKey.value) ?? tabs[0]);
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <div class="flex gap-0.5 rounded-control border border-line p-0.5">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-badge px-2.5 py-1 text-body-sm transition-colors"
          :class="
            activeKey === tab.key
              ? 'bg-brand text-white'
              : 'text-ink-soft hover:bg-line-soft hover:text-ink'
          "
          @click="activeKey = tab.key"
        >
          <component :is="tab.icon" :size="13" :stroke-width="1.9" />
          {{ t(tab.labelKey) }}
        </button>
      </div>
      <p class="min-w-0 flex-1 truncate text-caption text-muted">{{ t(active.hintKey) }}</p>
    </div>

    <KeepAlive>
      <component :is="active.component" />
    </KeepAlive>
  </div>
</template>
