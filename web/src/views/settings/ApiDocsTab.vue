<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ExternalLink, Loader2, RefreshCw, TriangleAlert } from "lucide-vue-next";
import { api, type DocsStatus } from "../../api";
import { t } from "../../lib/i18n";

/**
 * 接口文档 = 同源 iframe 内嵌后端自建的 `/docs`（Swagger UI）。
 *
 * 为什么先探测：`CHAIERWW_DISABLE_DOCS=1` 或静态资源缺失时 `/docs` 不可用，
 * 直接挂 iframe 会拿到 SPA 的 index.html —— 表现为「文档里套着整个应用」。
 * 所以先问 `/api/docs/status`，不可用就明说原因，不放 iframe。
 */
const status = ref<DocsStatus | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);
const frameKey = ref(0);

async function probe(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    status.value = await api.docsStatus();
  } catch (caught) {
    status.value = null;
    error.value = (caught as Error).message;
  } finally {
    loading.value = false;
  }
}

async function reload(): Promise<void> {
  await probe();
  frameKey.value += 1;
}

onMounted(probe);
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <div class="flex shrink-0 items-center gap-3 border-b border-line-soft px-4 py-2">
      <div class="ml-auto flex items-center gap-3">
        <a
          v-if="status?.available"
          :href="status.docsUrl"
          target="_blank"
          rel="noopener"
          class="inline-flex items-center gap-1 text-[12px] text-brand-dark hover:underline"
        >
          {{ t("settings.api.openInNew") }} <ExternalLink :size="12" />
        </a>
        <a
          :href="status?.openapiUrl ?? '/api/openapi.json'"
          target="_blank"
          rel="noopener"
          class="inline-flex items-center gap-1 font-mono text-[11.5px] text-muted hover:text-ink"
        >
          openapi.json
        </a>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-[8px] border border-line px-2 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
          @click="reload"
        >
          <RefreshCw :size="12" :class="loading ? 'animate-spin' : ''" /> {{ t("settings.api.reload") }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="flex flex-1 items-center justify-center gap-2 text-[13px] text-muted">
      <Loader2 :size="15" class="animate-spin" /> {{ t("settings.api.probing") }}
    </div>

    <div
      v-else-if="error || !status?.available"
      class="flex flex-1 items-center justify-center p-6"
    >
      <div class="max-w-[420px] rounded-[12px] border border-line bg-surface p-5 text-center">
        <TriangleAlert :size="20" class="mx-auto text-warn" />
        <p class="mt-2 text-[13px] font-medium text-ink">{{ t("settings.api.unavailable") }}</p>
        <p class="mt-1 text-[12px] leading-relaxed text-muted">
          {{ error || status?.reason || t("settings.api.reasonUnknown") }}
        </p>
        <p class="mt-2 text-[11.5px] text-muted">
          {{ t("settings.api.usable") }}
          <code class="font-mono">{{ status?.openapiUrl ?? "/api/openapi.json" }}</code>
          {{ t("settings.api.usableTail") }}
        </p>
      </div>
    </div>

    <iframe
      v-else
      :key="frameKey"
      :src="status.docsUrl"
      :title="t('settings.api.frameTitle')"
      class="min-h-0 w-full flex-1 border-0 bg-surface"
    />
  </div>
</template>
