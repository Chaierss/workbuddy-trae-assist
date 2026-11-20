<script setup lang="ts">
import ClientSettingsTab from "./ClientSettingsTab.vue";
import EnvironmentTab from "./EnvironmentTab.vue";

/**
 * 设置 → 客户端与环境：客户端 settings.json 的登记键，以及本机路径、客户端状态、
 * 凭据加密方式。原来两栏各占一格，实际排查时总是一起看，现在合成一页。
 */
</script>

<template>
  <div class="space-y-4">
    <ClientSettingsTab />
    <EnvironmentTab />
  </div>
</template>
