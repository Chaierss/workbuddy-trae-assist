<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { FileText, Loader2, RefreshCw } from "lucide-vue-next";
import { api, formatBytes, formatTime, type DocContent, type DocEntry } from "../../api";
import { t } from "../../lib/i18n";
import { md, isExternalHref, resolveDocHref } from "../../lib/markdown";

/**
 * Wiki = 仓库 docs/ 下的 Markdown，目录与正文都从后端接口读。
 * 不写死文件清单：新增一个 .md 刷新即出现，不用改前端。
 */
const route = useRoute();
const router = useRouter();

const files = ref<DocEntry[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const content = ref<DocContent | null>(null);
const contentLoading = ref(false);
const contentError = ref<string | null>(null);

const selected = computed(() => (typeof route.query.path === "string" ? route.query.path : ""));

const rendered = computed(() => (content.value ? md.render(content.value.content) : ""));

const meta = computed(() => files.value.find((item) => item.path === selected.value) ?? null);

const groups = computed(() => {
  const map = new Map<string, DocEntry[]>();
  for (const file of files.value) {
    const bucket = map.get(file.group) ?? [];
    bucket.push(file);
    map.set(file.group, bucket);
  }
  return [...map.entries()].map(([group, items]) => ({ group, items }));
});

async function loadFiles(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const data = await api.docs();
    files.value = data.files;
  } catch (caught) {
    error.value = (caught as Error).message;
  } finally {
    loading.value = false;
  }
}

async function loadDoc(path: string): Promise<void> {
  if (!path) {
    content.value = null;
    return;
  }
  contentLoading.value = true;
  contentError.value = null;
  try {
    content.value = await api.docContent(path);
  } catch (caught) {
    content.value = null;
    contentError.value = (caught as Error).message;
  } finally {
    contentLoading.value = false;
  }
}

/** 选中走 query：刷新后仍停在同一篇，也能直接分享链接。 */
function select(path: string): void {
  if (path === selected.value) return;
  void router.replace({ query: { ...route.query, path } });
}

/** 点 md 里的内链时自己接管，否则会被当成站外路径整页跳走。 */
function onArticleClick(event: MouseEvent): void {
  const anchor = (event.target as HTMLElement | null)?.closest("a");
  if (!anchor) return;
  const href = anchor.getAttribute("href") ?? "";
  const target = resolveDocHref(content.value?.path ?? "", href);
  if (target) {
    event.preventDefault();
    select(target);
    return;
  }
  if (isExternalHref(href)) {
    event.preventDefault();
    window.open(href, "_blank", "noopener");
  }
}

async function refresh(): Promise<void> {
  await loadFiles();
  if (selected.value) await loadDoc(selected.value);
}

/**
 * immediate 是必须的：直接打开 `?path=PLAN.md` 这类深链时，
 * onMounted 只负责「没选就选默认」，正文加载得靠这条 watch 立刻触发。
 */
watch(selected, (path) => void loadDoc(path), { immediate: true });

onMounted(async () => {
  await loadFiles();
  if (!selected.value) {
    const preferred = files.value.find((item) => item.path.toLowerCase() === "readme.md");
    const fallback = preferred ?? files.value[0];
    if (fallback) {
      await router.replace({ query: { ...route.query, path: fallback.path } });
      await nextTick();
    }
  }
});
</script>

<template>
  <div class="flex h-full min-h-0">
    <div class="flex w-[212px] shrink-0 flex-col border-r border-line-soft">
      <div class="flex shrink-0 items-center gap-2 px-3 py-2">
        <span class="flex-1 text-caption font-medium text-muted">{{ t("settings.wiki.toc") }}</span>
        <button
          type="button"
          class="grid size-6 place-items-center rounded-badge text-muted transition-colors hover:bg-line-soft hover:text-ink"
          :title="t('settings.wiki.reload')"
          @click="refresh"
        >
          <RefreshCw :size="12" :class="loading ? 'animate-spin' : ''" />
        </button>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        <p v-if="loading && !files.length" class="px-2 py-3 text-body-sm text-muted">
          {{ t("settings.wiki.reading") }}
        </p>
        <p v-else-if="error" class="px-2 py-3 text-body-sm text-danger">{{ error }}</p>
        <p v-else-if="!files.length" class="px-2 py-3 text-body-sm leading-relaxed text-muted">
          {{ t("settings.wiki.noDocs") }}
          <code class="font-mono">CHAIERWW_DOCS</code>
          {{ t("settings.wiki.noDocsTail") }}
        </p>

        <div v-for="entry in groups" :key="entry.group" class="mb-1.5 last:mb-0">
          <div v-if="entry.group" class="px-2 py-1 font-mono text-micro uppercase text-muted">
            {{ entry.group }}
          </div>
          <button
            v-for="file in entry.items"
            :key="file.path"
            type="button"
            class="flex w-full items-start gap-1.5 rounded-control px-2 py-1.5 text-left text-body-sm transition-colors"
            :class="
              selected === file.path
                ? 'bg-brand-soft font-medium text-brand-dark'
                : 'text-ink-soft hover:bg-line-soft hover:text-ink'
            "
            @click="select(file.path)"
          >
            <FileText :size="13" class="mt-0.5 shrink-0 opacity-60" />
            <span class="min-w-0 flex-1 truncate" :title="file.path">{{ file.title }}</span>
          </button>
        </div>
      </div>
    </div>

    <article class="min-w-0 flex-1 overflow-y-auto">
      <header
        class="sticky top-0 z-[1] flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line-soft bg-surface px-6 py-2.5"
      >
        <h2 class="text-body font-semibold text-ink">
          {{ content?.title ?? t("settings.wiki.selectDoc") }}
        </h2>
        <span v-if="content" class="font-mono text-caption text-muted">{{ content.path }}</span>
        <span v-if="meta" class="ml-auto text-caption text-muted">
          {{ formatTime(meta.updatedAt) }} · {{ formatBytes(meta.size) }}
        </span>
      </header>

      <div v-if="contentLoading" class="flex items-center gap-2 px-6 py-10 text-body text-muted">
        <Loader2 :size="14" class="animate-spin" /> {{ t("common.loading") }}
      </div>
      <p v-else-if="contentError" class="px-6 py-10 text-body text-danger">{{ contentError }}</p>
      <div v-else-if="rendered" class="doc-prose px-6 py-5" @click="onArticleClick" v-html="rendered" />
      <p v-else class="px-6 py-10 text-body text-muted">{{ t("settings.wiki.pickDoc") }}</p>
    </article>
  </div>
</template>
