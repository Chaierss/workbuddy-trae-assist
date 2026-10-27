; 安装/卸载时的自定义 NSIS 脚本。
; electron-builder 默认就会 include build/installer.nsh，不用额外配置。
;
; 干什么：
;   1. 把 chaierww-backend.exe 也收掉（见下方 preInit / customUnInit 的说明）
;   2. 卸载确认与「数据会保留」说明放在卸载向导的欢迎页（customUnInit 只收后端进程）
;   3. 定各个向导页的文案（欢迎页 / 目录页 / 完成页），并把欢迎页补回来
;   4. 跳过「为所有用户 / 仅为我安装」那一页（customInstallMode）
;   5. 记住上次安装位置，卸载后重装仍默认落在原目录（customInstall / preInit）
;
; 注意：本文件被前插在 !include MUI2.nsh 之前，只能用 NSIS 核心指令
; （IfSilent / IntCmp / MessageBox / StrCpy…），不能用 LogicLib 的 ${If} 语法。
;
; 为什么收后端：NSIS 默认的「先关掉正在运行的应用」只认 APP_EXECUTABLE_FILENAME
; （chaierww.exe），完全不认识后端进程。而后端是 PyInstaller --onefile 打的
; （引导器 + 子进程两层），旧版被 /f 强杀时子进程会变孤儿，继续锁着
; resources/backend/chaierww-backend.exe。安装程序解压新包时 7z 覆盖不了这个
; 被锁的文件，就弹「chaierww 无法关闭」，而它的重试只查 chaierww.exe（早没了），
; 于是永远重试不过去 —— 重装就这么卡死。
;
; preInit 在 .onInit 最前面执行，早于 NSIS 的进程检查和解压。
;
; 两个分支不一样：
;   - 安装：只在「主程序没在跑、后端却在跑」时动手。这组合就是旧版留下的孤儿。
;     主程序正常开着的话交给默认的「请关闭 chaierww」流程，不该提前把它的后端打死。
;   - 卸载：应用本来就该关掉，直接收。

; ——— 向导各页文案 ———
;
; 这些不是 MUI 的宏而是「默认值」：MUI2 内部是
; `!insertmacro MUI_DEFAULT MUI_WELCOMEPAGE_TEXT "$(MUI_TEXT_WELCOME_INFO_TEXT)"`，
; 只要在 include MUI2 之前先 !define，MUI_DEFAULT 就不会覆盖。所以既不用自己写
; customWelcomePage / customFinishPage 宏（那会整段替换模板、连带丢掉 StartApp，
; 升级 electron-builder 就炸），也不用担心被模板改回去。
;
; 语言（简体中文）在 package.json 的 build.nsis.language 里定，这里只管正文。
!ifndef ONE_CLICK
  ; 欢迎页：一次说清「装什么 / 装在哪 / 数据在哪 / 要不要管理员」。
  !define MUI_WELCOMEPAGE_TITLE "安装 chaierww"
  !define MUI_WELCOMEPAGE_TEXT "把 WorkBuddy / Trae 的多个账号收在一处：一键切号、签到积分、账号体检、用量统计。$\r$\n$\r$\n只为当前用户安装，不需要管理员权限，也不写系统目录。$\r$\n账号、凭据、备份与日志都在 $PROFILE\.chaierww —— 升级和卸载都不会动它。"

  ; 目录页：用户最容易把「安装目录」当成「数据目录」，这里当场分开讲。
  ; 「默认沿用上次位置」由下方 customInstall / preInit 的 LastInstallDir 保证。
  !define MUI_DIRECTORYPAGE_TEXT_TOP "程序文件装到这里（约 130 MB）。默认沿用上次安装位置，要换可以直接改。$\r$\n账号数据不在这里，在 $PROFILE\.chaierww；换安装位置不影响已有账号。"
  !define MUI_DIRECTORYPAGE_TEXT_DESTINATION "安装到："

  ; 完成页：默认文本区只有 40u（约 3 行），长文案会被裁掉，所以打开 TEXT_LARGE（60u）。
  !define MUI_FINISHPAGE_TEXT_LARGE
  !define MUI_FINISHPAGE_TEXT "chaierww 已安装，并常驻系统托盘。$\r$\n关闭窗口只是收进托盘，退出请用托盘右键 →「退出」。$\r$\n$\r$\n账号与凭据保存在 $PROFILE\.chaierww，卸载不会删除。$\r$\n详见安装目录下的 INSTALL.txt 与 DISCLAIMER.md。"
  !define MUI_FINISHPAGE_RUN_TEXT "立即运行 chaierww"
  ; 完成页底部的小链接（点击时按运行期展开，所以 $INSTDIR 指向真实安装位置）。
  !define MUI_FINISHPAGE_LINK "打开使用说明（INSTALL.txt）"
  !define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\INSTALL.txt"

  ; 解压中误点「取消」时先确认一次，避免留下半套文件。
  !define MUI_ABORTWARNING

  ; —— 卸载向导文案 ——
  ; 模板只插「欢迎 / 卸载中 / 完成」三页；确认交给欢迎页，不再额外弹 MessageBox。
  ; 静默卸载（更新器带 /S）本来就不显示页面，因此这里不会干扰自动更新。
  !define MUI_UNWELCOMEPAGE_TITLE "卸载 chaierww"
  !define MUI_UNWELCOMEPAGE_TEXT "这次只删除程序文件、桌面快捷方式和开始菜单项。$\r$\n$\r$\n会保留：$PROFILE\.chaierww 里的账号、凭据、备份、日志与网页版登录态；$APPDATA\chaierww 里的更新偏好。$\r$\n重装后旧账号会自动回来。要彻底清干净，请在卸载完成后手动删除 $PROFILE\.chaierww。"
  !define MUI_UNABORTWARNING
  !define MUI_UNFINISHPAGE_TEXT "chaierww 已卸载。$\r$\n$\r$\n账号、凭据与备份仍保留在 $PROFILE\.chaierww，重装后会回来；要彻底清理请手动删除该目录。"
!endif

; 跳过「Choose Installation Options / 为所有用户还是仅为我」这一页。
;
; 本软件把程序装到当前用户目录、注册表只写 HKCU（perMachine 未开），
; 「为所有用户安装」对它没有实际意义，却会多一次 UAC 提权弹窗和一次选择。
; electron-builder 的 multiUserUi.nsh 里有 `!ifmacrodef customInstallMode` 钩子，
; 把 $isForceCurrentInstall 置 1 后该页在 PRE 回调里直接 Abort（等于跳过），
; 并按当前用户安装。这也是官方文档给 per-user 应用的标准做法。
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
!macroend

; 让欢迎页真的出现。
;
; electron-builder 的 assisted installer 默认**不插** MUI_PAGE_WELCOME（模板里只有
; 许可 / 安装范围 / 目录 / 安装中 / 完成五页），所以 MUI_WELCOMEPAGE_* 这组 define
; 在没插页之前是不生效的死配置 —— 用户打开安装包第一屏直接是一大段许可条款。
; 这里通过官方的 customWelcomePage 钩子把**标准**欢迎页补回去：只是多插一页，
; 不接管向导流程，因此不会像自定义页面那样一升级 electron-builder 就炸。
!macro customWelcomePage
  !insertmacro MUI_PAGE_WELCOME
!macroend

; 记住这次装到哪。
;
; electron-builder 卸载时会把 HKCU\Software\<APP_GUID> 整键删掉（InstallLocation
; 就在里面），重装读不到就回落到 $LocalAppData\Programs\chaierww —— 这是「重装
; 路径被改回默认」的根因。这里把最终路径另存到一个卸载不会动的键里；
; preInit 在重装时再回填回去。只存路径字符串，不放任何账号数据。
!macro customInstall
  WriteRegStr HKCU "Software\Chaierss\chaierww" LastInstallDir "$INSTDIR"
!macroend

!macro preInit
  ; —— 重装沿用上次安装位置 ——
  ;
  ; 必须在这里（早于 initMultiUser 和安装范围页的 PRE 回调）回填官方键：
  ; 安装范围页虽然被 customInstallMode 跳过，PRE 回调里仍会再调一次
  ; setInstallModePerUser，只在 customInit 里改 $INSTDIR 会被它重置。
  ;
  ; 只回填 InstallLocation，不写 UninstallString —— 否则安装器会去找一个
  ; 已经不存在的旧卸载器。BUILD_UNINSTALLER 那次编译不执行，避免污染构建机。
  !ifndef BUILD_UNINSTALLER
    ReadRegStr $R0 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
    StrCmp $R0 "" 0 cw_preinit_path_ready
    ReadRegStr $R0 HKCU "Software\Chaierss\chaierww" LastInstallDir
    StrCmp $R0 "" 0 cw_preinit_path_write
    Goto cw_preinit_path_ready
    cw_preinit_path_write:
      WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation "$R0"
    cw_preinit_path_ready:
  !endif

  ; tasklist 加 /FI 没匹配到时退出码仍是 0，所以拿 find 的退出码判断
  ; （找到 → 0，没找到 → 1）。
  nsExec::ExecToStack '"$SYSDIR\cmd.exe" /c tasklist /FI "IMAGENAME eq chaierww.exe" /NH | "$SYSDIR\find.exe" /I "chaierww.exe"'
  Pop $0
  Pop $1
  IntCmp $0 0 preInit_done preInit_check_orphan preInit_check_orphan

  preInit_check_orphan:
    nsExec::ExecToStack '"$SYSDIR\cmd.exe" /c tasklist /FI "IMAGENAME eq chaierww-backend.exe" /NH | "$SYSDIR\find.exe" /I "chaierww-backend.exe"'
    Pop $2
    Pop $3
    IntCmp $2 0 preInit_kill preInit_done preInit_done

  preInit_kill:
    DetailPrint "清理旧版残留的后端进程…"
    nsExec::Exec '"$SYSDIR\cmd.exe" /c taskkill /F /IM "chaierww-backend.exe"'
    Pop $4
    Sleep 600

  preInit_done:
!macroend

!macro customUnInit
  ; 卸载确认交给卸载向导的欢迎页（MUI_UNWELCOMEPAGE_TEXT 里已写明删什么、留什么），
  ; 这里不再弹 MessageBox —— 那会让用户先确认一次、进向导再点一次，很啰嗦。
  ; 静默卸载（electron-updater 带 /S）本来就不显示页面，因此不会干扰自动更新。
  ;
  ; 只收掉后端进程：PyInstaller --onefile 是「引导器 + 子进程」两层，
  ; 旧版被强杀时子进程会变孤儿并锁住安装目录里的文件。
  nsExec::Exec '"$SYSDIR\cmd.exe" /c taskkill /F /IM "chaierww-backend.exe"'
  Pop $0
  Sleep 400
!macroend
