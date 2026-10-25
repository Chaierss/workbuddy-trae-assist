<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { Loader2, RefreshCw, TriangleAlert } from "lucide-vue-next";
import { api, formatTime } from "../../api";
import OpenApiViewer from "../../components/OpenApiViewer.vue";
import { t } from "../../lib/i18n";
import type { OpenApiSpec } from "../../lib/openapi";

/**
 * 接口文档 = 直接读 `/api/openapi.json` 并原生渲染。
 *
 * 以前内嵌后端自建的 Swagger UI，桌面安装包没带静态资源就整页不可用；
 * 规格本身一直可用，所以这里改成「有 json 就能看」。刷新是自动的：
 * 打开时、窗口重新获得焦点时、以及每 5 分钟（页面隐藏时跳过）各拉一次。
 * 不做任何文档生成，规格由后端运行期产出。
 */
const spec = ref<OpenApiSpec | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);
const updatedAt = ref("");

const REFRESH_MS = 5 * 60 * 1000;
let timer: number | undefined;

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true;
  try {
    spec.value = await api.openapi();
    error.value = null;
    updatedAt.value = new Date().toISOString();
  } catch (caught) {
    error.value = (caught as Error).message;
  } finally {
    loading.value = false;
  }
}

function onFocus(): void {
  void load(true);
}

function onTimer(): void {
  if (document.hidden) return;
  void load(true);
}

onMounted(() => {
  void load();
  window.addEventListener("focus", onFocus);
  timer = window.setInterval(onTimer, REFRESH_MS);
});

onBeforeUnmount(() => {
  window.removeEventListener("focus", onFocus);
  if (timer !== undefined) window.clearInterval(timer);
});
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <div
      class="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line-soft px-4 py-2"
    >
      <span class="text-body-sm font-semibold text-ink">
        {{ spec?.info?.title ?? t("settings.api.label") }}
      </span>
      <span
        v-if="spec?.info?.version"
        class="rounded-full bg-line-soft px-2 py-0.5 font-mono text-micro text-ink-soft"
      >
        {{ t("settings.api.version", { version: spec.info.version }) }}
      </span>
      <span class="text-caption text-muted">{{ t("settings.api.autoRefresh") }}</span>
      <span v-if="updatedAt" class="text-caption text-muted">
        {{ t("settings.api.updatedAt", { time: formatTime(updatedAt) }) }}
      </span>

      <div class="ml-auto flex items-center gap-3">
        <a
          href="/api/openapi.json"
          target="_blank"
          rel="noopener"
          class="font-mono text-caption text-muted hover:text-ink"
        >
          openapi.json
        </a>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-control border border-line px-2 py-1 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
          @click="load()"
        >
          <RefreshCw :size="12" :class="loading ? 'animate-spin' : ''" />
          {{ t("settings.api.reload") }}
        </button>
      </div>
    </div>

    <div
      v-if="loading && !spec"
      class="flex flex-1 items-center justify-center gap-2 text-body text-muted"
    >
      <Loader2 :size="15" class="animate-spin" /> {{ t("settings.api.loading") }}
    </div>

    <div v-else-if="!spec" class="flex flex-1 items-center justify-center p-6">
      <div class="max-w-[420px] rounded-card border border-line bg-surface p-5 text-center">
        <TriangleAlert :size="20" class="mx-auto text-warn" />
        <p class="mt-2 text-body font-medium text-ink">{{ t("settings.api.loadFailed") }}</p>
        <p class="mt-1 text-body-sm leading-relaxed text-muted">
          {{ error || t("settings.api.reasonUnknown") }}
        </p>
        <a
          href="/api/openapi.json"
          target="_blank"
          rel="noopener"
          class="mt-2 inline-block font-mono text-caption text-brand-dark hover:underline"
        >
          /api/openapi.json
        </a>
      </div>
    </div>

    <div v-else class="min-h-0 flex-1">
      <OpenApiViewer class="h-full" :spec="spec" />
    </div>
  </div>
</template>
