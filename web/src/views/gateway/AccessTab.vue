<script setup lang="ts">
import { computed } from "vue";
import { Check, Copy } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import { t, type MessageKey } from "../../lib/i18n";
import { useGateway } from "./useGateway";

/** 接入信息：把端点复制到支持自定义 base_url 的客户端里。 */
const store = useGateway();

interface EndpointRow {
  tag: string;
  labelKey: MessageKey;
  value: string;
  hintKey: MessageKey;
}

/** 端点清单放在 computed 里：常量里的 t() 只在加载时求值一次，切语言不会更新。 */
const endpoints = computed<EndpointRow[]>(() => {
  const status = store.status;
  if (!status) return [];
  return [
    {
      tag: "base",
      labelKey: "gateway.access.baseLabel",
      value: `${status.baseUrl}/v1`,
      hintKey: "gateway.access.baseHint",
    },
    {
      tag: "root",
      labelKey: "gateway.access.rootLabel",
      value: status.baseUrl,
      hintKey: "gateway.access.rootHint",
    },
    {
      tag: "chat",
      labelKey: "gateway.access.chatLabel",
      value: status.chatUrl,
      hintKey: "gateway.access.chatHint",
    },
    {
      tag: "responses",
      labelKey: "gateway.access.responsesLabel",
      value: `${status.baseUrl}/v1/responses`,
      hintKey: "gateway.access.responsesHint",
    },
    {
      tag: "messages",
      labelKey: "gateway.access.messagesLabel",
      value: `${status.baseUrl}/v1/messages`,
      hintKey: "gateway.access.messagesHint",
    },
    {
      tag: "models",
      labelKey: "gateway.access.modelsLabel",
      value: status.modelsUrl,
      hintKey: "gateway.access.modelsHint",
    },
  ];
});
</script>

<template>
  <div v-if="!store.status" class="py-8 text-center text-body-sm text-muted">
    {{ t("gateway.loading") }}
  </div>

  <SectionCard v-else :title="t('gateway.access.title')" :subtitle="t('gateway.access.subtitle')">
    <div class="space-y-3">
      <div
        v-for="item in endpoints"
        :key="item.tag"
        class="rounded-control border border-line-soft px-3.5 py-2.5"
      >
        <div class="flex items-center justify-between gap-2">
          <span class="text-body-sm font-medium text-ink">{{ t(item.labelKey) }}</span>
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-control px-2 py-1 text-caption text-muted transition-colors hover:bg-line-soft hover:text-ink"
            @click="store.copy(item.value, item.tag)"
          >
            <component :is="store.copied === item.tag ? Check : Copy" :size="13" :stroke-width="1.9" />
            {{ store.copied === item.tag ? t("common.copied") : t("common.copy") }}
          </button>
        </div>
        <div class="mt-1 truncate font-mono text-body-sm text-ink-soft">{{ item.value }}</div>
        <div class="mt-0.5 text-caption text-muted">{{ t(item.hintKey) }}</div>
      </div>

      <p class="text-caption leading-relaxed text-muted">
        {{ t("gateway.access.note1") }} <code>deepseek-v4.1-flash</code>{{ t("gateway.access.note2") }} <code>Authorization: Bearer sk-…</code>{{ t("gateway.access.note3") }}
        <span v-if="!store.status.loopbackOnly" class="text-warn">{{ t("gateway.access.nonLoopbackWarn") }}</span>
      </p>
    </div>
  </SectionCard>
</template>
