/**
 * electron-builder afterPack 钩子：给 Windows 应用主程序写入图标与版本信息。
 *
 * 为什么需要它：
 *   electron-builder 官方的 rcedit 步骤（win.signAndEditExecutable）会去
 *   GitHub 下载 winCodeSign-2.6.0.7z。该压缩包里含 macOS 符号链接，Windows
 *   非管理员账户无法解压（Cannot create symbolic link : 客户端没有所需的特权），
 *   导致打包必失败。由于本项目没有代码签名证书（签名本来就是空操作），
 *   我们在 package.json 里设了 "win": { "signAndEditExecutable": false }，
 *   改由这个钩子调用随仓库携带的 rcedit-x64.exe 完成同样的资源编辑。
 *
 * 携带的 rcedit：build/tools/rcedit-x64.exe（来自 electron-builder 官方
 * winCodeSign-2.6.0 包，未做任何修改）。
 */

"use strict";

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== "win32") {
    return;
  }

  const desktopRoot = path.join(__dirname, "..");
  const rcedit = path.join(desktopRoot, "build", "tools", "rcedit-x64.exe");
  const icon = path.join(desktopRoot, "build", "icon.ico");
  const exe = path.join(context.appOutDir, "chaierww.exe");

  if (!fs.existsSync(rcedit)) {
    console.warn(`[afterPack] 找不到 rcedit，跳过图标注入：${rcedit}`);
    return;
  }
  if (!fs.existsSync(exe)) {
    console.warn(`[afterPack] 找不到应用主程序，跳过图标注入：${exe}`);
    return;
  }

  const version = require(path.join(desktopRoot, "package.json")).version;
  const args = [
    exe,
    "--set-icon", icon,
    "--set-version-string", "CompanyName", "Chaierss",
    "--set-version-string", "FileDescription", "ChaierSS 多客户端切号管家",
    "--set-version-string", "ProductName", "ChaierSS",
    "--set-version-string", "InternalName", "chaierww",
    "--set-version-string", "OriginalFilename", "chaierww.exe",
    "--set-version-string", "LegalCopyright", "Copyright (c) 2025 Chaierss",
    "--set-file-version", version,
    "--set-product-version", version,
  ];

  execFileSync(rcedit, args, { stdio: "inherit" });
  console.log(`[afterPack] 已写入图标与版本信息（v${version}）：${exe}`);
};
