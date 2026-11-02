; 安装/卸载时的自定义 NSIS 脚本。
; electron-builder 默认就会 include build/installer.nsh，不用额外配置。
;
; 干什么：
;   1. 把 chaierww-backend.exe 也收掉（见下方 preInit / customUnInit 的说明）
;   2. 重装（已装过）自动切到精简升级流程：以官方 --updated 模式重启自己，
;      跳过协议页与安装目录页，只显示一页「升级/修复」信息（customWelcomePage）
;   3. 卸载页由用户决定要不要删除我的资料（customUnWelcomePage 的勾选框），
;      customUnInstall 按选择删 $PROFILE\.chaierww 与 $APPDATA\chaierww；静默卸载永不删
;   4. 定各个向导页的文案（欢迎页 / 目录页 / 完成页），并把欢迎页补回来
;   5. 跳过「为所有用户 / 仅为我安装」那一页（customInstallMode）
;   6. 记住上次安装位置，卸载后重装仍默认落在原目录（customInstall / preInit）
;
; 注意：本文件被前插在 !include MUI2.nsh 之前。顶层的 !include / !define / Var 会被立刻处理，
; 所以顶层代码不要引用模板 define（${INSTALL_REGISTRY_KEY} 等）或 MUI / LogicLib 宏；
; 需要它们的代码放进 !macro 体内 —— 宏在页面插入点展开，那时 MUI2 与 multiUser.nsh 都已就位。
; 下面的 !include nsDialogs.nsh 会顺带引入 LogicLib 与 WinMessages，供自绘页面使用。
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

!include nsDialogs.nsh

; 宏体内共享的全局变量（顶层 Var 不依赖模板 define）。
;
; 变量必须按编译阶段声明：BUILD_UNINSTALLER 那一次只编译卸载器代码，
; 安装器专用的变量会变成「声明了却没人用」（warning 6001，-WX 下直接报错）。
!ifndef BUILD_UNINSTALLER
  Var /GLOBAL cwOldVersion      ; 已安装版本（用于升级页显示）
  Var /GLOBAL cwOldInstallDir   ; 已安装位置（用于升级页显示）
!endif

!ifdef BUILD_UNINSTALLER
  Var /GLOBAL cwDeleteUserData  ; 卸载时是否删除资料：1 = 删除，0 = 保留
  Var /GLOBAL cwDeleteCheckbox  ; 卸载页勾选框句柄
!endif

; ——— 向导各页文案 ———
;
; 这些不是 MUI 的宏而是「默认值」：MUI2 内部是
; `!insertmacro MUI_DEFAULT MUI_WELCOMEPAGE_TEXT "$(MUI_TEXT_WELCOME_INFO_TEXT)"`，
; 只要在 include MUI2 之前先 !define，MUI_DEFAULT 就不会覆盖。所以既不用自己写
; customFinishPage 宏（那会整段替换模板、连带丢掉 StartApp，
; 升级 electron-builder 就炸），也不用担心被模板改回去。
;
; 语言（简体中文）在 package.json 的 build.nsis.language 里定，这里只管正文。
!ifndef ONE_CLICK
  ; 欢迎页：一次说清「装什么 / 装在哪 / 数据在哪 / 要不要管理员」。
  ; 只在首次安装显示；重装走 --updated 升级流程，改由下方的升级信息页接管。
  !define MUI_WELCOMEPAGE_TITLE "安装 chaierww"
  !define MUI_WELCOMEPAGE_TEXT "把 WorkBuddy / Trae 的多个账号收在一处：一键切号、签到积分、账号体检、用量统计。$\r$\n$\r$\n只为当前用户安装，不需要管理员权限，也不写系统目录。$\r$\n账号、凭据、备份与日志都在 $PROFILE\.chaierww —— 升级和卸载都不会动它。"

  ; 目录页：用户最容易把「安装目录」当成「数据目录」，这里当场分开讲。
  ; 「默认沿用上次位置」由下方 customInstall / preInit 的 LastInstallDir 保证。
  !define MUI_DIRECTORYPAGE_TEXT_TOP "程序文件装到这里（约 130 MB）。默认沿用上次安装位置，要换可以直接改。$\r$\n账号数据不在这里，在 $PROFILE\.chaierww；换安装位置不影响已有账号。"
  !define MUI_DIRECTORYPAGE_TEXT_DESTINATION "安装到："

  ; 完成页：默认文本区只有 40u（约 3 行），长文案会被裁掉，所以打开 TEXT_LARGE（60u）。
  !define MUI_FINISHPAGE_TEXT_LARGE
  !define MUI_FINISHPAGE_TEXT "chaierww 已安装，并常驻系统托盘。$\r$\n关闭窗口只是收进托盘，退出请用托盘右键 →「退出」。$\r$\n$\r$\n账号与凭据保存在 $PROFILE\.chaierww，覆盖安装不会删除。$\r$\n详见安装目录下的 INSTALL.txt 与 DISCLAIMER.md。"
  !define MUI_FINISHPAGE_RUN_TEXT "立即运行 chaierww"
  ; 完成页底部的小链接（点击时按运行期展开，所以 $INSTDIR 指向真实安装位置）。
  !define MUI_FINISHPAGE_LINK "打开使用说明（INSTALL.txt）"
  !define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\INSTALL.txt"

  ; 解压中误点「取消」时先确认一次，避免留下半套文件。
  !define MUI_ABORTWARNING

  ; —— 卸载向导 ——
  ; 欢迎页已被 customUnWelcomePage 的自绘页替换（含「是否删除资料」勾选框），
  ; 所以不再需要 MUI_UNWELCOMEPAGE_* 文案。
  !define MUI_UNABORTWARNING
  !define MUI_UNFINISHPAGE_TEXT "chaierww 已卸载。$\r$\n$\r$\n账号与资料已按上一步的选择处理：未勾选「同时删除我的资料」时，内容仍保留在 $PROFILE\.chaierww，重装后会自动回来。"
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

; 让欢迎页真的出现，并在重装时换成「升级/修复」信息页。
;
; electron-builder 的 assisted installer 默认**不插** MUI_PAGE_WELCOME（模板里只有
; 许可 / 安装范围 / 目录 / 安装中 / 完成五页），所以 MUI_WELCOMEPAGE_* 这组 define
; 在没插页之前是不生效的死配置 —— 用户打开安装包第一屏直接是一大段许可条款。
; 这里通过官方的 customWelcomePage 钩子把**标准**欢迎页补回去：只是多插一页，
; 不接管向导流程，因此不会像自定义页面那样一升级 electron-builder 就炸。
;
; 两页的分工：
;   - 标准欢迎页：首次安装显示；带 --updated（重装 / 自动更新）时由 PRE 回调 Abort 跳过。
;   - 升级信息页：只在 --updated 时显示；首次安装时 create 里 Abort 跳过。
;
; 函数必须定义在宏体内：宏在 assistedInstaller.nsh 里展开，此时 MUI2 / LogicLib 已就位，
; 顶层直接写 Function 会在编译期找不到 MUI_HEADER_TEXT、${If} 等宏。
!macro customWelcomePage
  !define MUI_PAGE_CUSTOMFUNCTION_PRE cwSkipWelcomeWhenUpdated

  Function cwSkipWelcomeWhenUpdated
    ${If} ${isUpdated}
      Abort
    ${EndIf}
  FunctionEnd

  !insertmacro MUI_PAGE_WELCOME

  Function cwUpgradePageCreate
    ${IfNot} ${isUpdated}
      Abort
    ${EndIf}

    !insertmacro MUI_HEADER_TEXT "升级 chaierww" "检测到已安装版本，本次为覆盖安装，账号与资料不受影响。"

    ${If} $cwOldVersion == ""
      StrCpy $cwOldVersion "未知"
    ${EndIf}

    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
      Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 18u "已安装版本：$cwOldVersion"
    Pop $0
    ${NSD_CreateLabel} 0 22u 100% 18u "本次安装版本：${VERSION}"
    Pop $0
    ${NSD_CreateLabel} 0 46u 100% 18u "安装位置：$cwOldInstallDir"
    Pop $0
    ${NSD_CreateLabel} 0 72u 100% 36u "账号、凭据、备份与日志保存在 $PROFILE\.chaierww，覆盖安装不会改动它们。"
    Pop $0

    nsDialogs::Show
  FunctionEnd

  Function cwUpgradePageLeave
  FunctionEnd

  Page custom cwUpgradePageCreate cwUpgradePageLeave
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

  ; —— 已安装 → 自动切到官方「升级」流程 ——
  ;
  ; 模板只认命令行里的 --updated：它会用 skipPageIfUpdated 跳过协议页与安装目录页。
  ; 手动双击安装包不带这个参数，所以在这里检测到已安装就带着 --updated 重启自己：
  ; 新实例看到 ${isUpdated} 就不会再重启，形成「升级信息 → 安装中 → 完成」三页。
  ;
  ; 静默安装（/S）不重启，保持无人值守语义；Exec 失败（被安全软件拦截等）也不阻断，
  ; 继续走完整五页向导，避免用户卡在「什么都没发生」。
  ;
  ; 只回填/重启，不在这里杀进程：主程序开着时交给 NSIS 默认的「请关闭」流程，
  ; 孤儿后端的清理在下面处理。
  !ifndef BUILD_UNINSTALLER
    ReadRegStr $R1 HKCU "${UNINSTALL_REGISTRY_KEY}" DisplayVersion
    StrCpy $cwOldVersion $R1
    ReadRegStr $R2 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
    StrCpy $cwOldInstallDir $R2

    ${IfNot} ${isUpdated}
      ${AndIfNot} ${Silent}
        ${If} $R2 != ""
          ClearErrors
          Exec '"$EXEPATH" --updated'
          ${IfNot} ${Errors}
            SetErrorLevel 0
            Quit
          ${EndIf}
        ${EndIf}
    ${EndIf}
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
  ; 资料默认保留：静默卸载（electron-updater 带 /S）不显示勾选页，
  ; 这里的初始值决定它绝不能误删。
  StrCpy $cwDeleteUserData "0"

  ; 卸载确认与「是否删除资料」都在 customUnWelcomePage 的勾选页里，
  ; 这里不再弹 MessageBox —— 那会让用户先确认一次、进向导再点一次，很啰嗦。
  ;
  ; 只收掉后端进程：PyInstaller --onefile 是「引导器 + 子进程」两层，
  ; 旧版被强杀时子进程会变孤儿并锁住安装目录里的文件。
  nsExec::Exec '"$SYSDIR\cmd.exe" /c taskkill /F /IM "chaierww-backend.exe"'
  Pop $0
  Sleep 400
!macroend

; 按用户选择删除资料。展开在卸载 Section 内、模板删程序文件之前，
; 进度页能看到这一行；RMDir /r 失败（个别文件被占用）不会中断卸载。
;
; HKCU\Software\Chaierss\chaierww 的 LastInstallDir 只是路径记忆，不是资料，保留。
!macro customUnInstall
  ${If} $cwDeleteUserData == "1"
    DetailPrint "按用户选择删除资料目录…"
    RMDir /r "$PROFILE\.chaierww"
    RMDir /r "$APPDATA\chaierww"
  ${Else}
    DetailPrint "按用户选择保留资料目录 $PROFILE\.chaierww"
  ${EndIf}
!macroend

; 卸载欢迎页替换成「资料处置」勾选页。
;
; 模板里这是官方钩子：定义了 customUnWelcomePage 就不再插默认的 MUI_UNPAGE_WELCOME。
; 先补一次 MUI_UNPAGE_INIT，保证 MUI 卸载器前缀与界面变量在本页编译时就已就位。
; 页面顺序仍是「勾选页 → 卸载中 → 完成」，页数不变。
!macro customUnWelcomePage
  !insertmacro MUI_UNPAGE_INIT

  Function un.cwUnDataPageCreate
    !insertmacro MUI_HEADER_TEXT "卸载 chaierww" "先确认资料的处置方式，再开始卸载。"

    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
      Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 18u "将删除程序文件、桌面快捷方式和开始菜单项。"
    Pop $0
    ${NSD_CreateLabel} 0 22u 100% 42u "你的资料默认保留在 $PROFILE\.chaierww：$\r$\n账号、凭据、备份、日志与网页版登录态都在这里，重装后会自动回来。"
    Pop $0
    ${NSD_CreateCheckbox} 0 70u 100% 16u "同时删除我的资料（含 $APPDATA\chaierww 的更新偏好；不可恢复）"
    Pop $cwDeleteCheckbox

    ; 按 $cwDeleteUserData 恢复勾选状态（默认 "0" = 不勾）。
    ${If} $cwDeleteUserData == "1"
      ${NSD_Check} $cwDeleteCheckbox
    ${Else}
      ${NSD_Uncheck} $cwDeleteCheckbox
    ${EndIf}

    ; 「下一步」按钮改成「卸载」，避免用户以为只是翻页。
    GetDlgItem $0 $HWNDPARENT 1
    SendMessage $0 ${WM_SETTEXT} 0 "STR:卸载"

    nsDialogs::Show
  FunctionEnd

  Function un.cwUnDataPageLeave
    ${NSD_GetState} $cwDeleteCheckbox $0
    ${If} $0 == ${BST_CHECKED}
      StrCpy $cwDeleteUserData "1"
    ${Else}
      StrCpy $cwDeleteUserData "0"
    ${EndIf}
  FunctionEnd

  ; 卸载页必须用 PageEx + 前缀 un. 声明；写成 `Page custom un.xxx` 会被当成「安装器页面」
  ; 引用，卸载器编译阶段（BUILD_UNINSTALLER）就判定回调未引用，`-WX` 直接报错。
  PageEx un.custom
    PageCallbacks un.cwUnDataPageCreate un.cwUnDataPageLeave
  PageExEnd
!macroend
